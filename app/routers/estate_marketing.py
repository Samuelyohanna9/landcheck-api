from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from geoalchemy2.shape import to_shape
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateAllocation,
    EstateCommissionPayout,
    EstateOrganization,
    EstateOrganizationMember,
    EstatePlot,
    EstatePublicReservationRequest,
    EstateQrCampaign,
)
from app.models.estate_marketing import EstateInspectionBooking, EstateInspectionSlot, EstateProgressUpdate, EstatePublicEvent
from app.routers.estates import _agent_display_name, _agent_portal_context, _public_estate
from app.routers.plots import get_db
from app.schemas.estate_marketing import (
    AgentBookingUpdate,
    AgentLeadUpdate,
    InspectionBookingCreate,
    InspectionSlotCreate,
    InspectionSlotUpdate,
    InspectionBookingUpdate,
    ProgressUpdatePatch,
    PublicEventCreate,
)
from app.services.estates import commissions, marketing_alerts, marketing_field, marketing_pdf, marketing_render
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal, require_estate_access
from app.services.estates.marketing_common import (
    agent_member_for,
    client_ip,
    ensure_agent_campaign,
    normalize_phone_digits,
    public_page_url,
    resolve_campaign,
    share_page_url,
    throttled,
    web_url,
    api_url,
    whatsapp_link,
)
from app.services.estates.operations import hash_portal_token
from app.services.estates.permissions import has_permission
from app.utils.r2_objects import build_r2_settings, delete_object_best_effort

router = APIRouter(prefix="/estates", tags=["estate-marketing"])

AD_FORMATS = {"status", "post", "landscape"}
NO_CACHE_PRIVATE = {"Cache-Control": "private, no-store"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ad_format(value: str) -> str:
    fmt = str(value or "post").strip().lower()
    if fmt not in AD_FORMATS:
        raise HTTPException(422, "Choose status, post or landscape")
    return fmt


def _estate_or_404(db: Session, estate_id: int) -> Estate:
    estate = db.get(Estate, estate_id)
    if estate is None or estate.archived_at is not None:
        raise HTTPException(404, "Estate not found")
    return estate


def _require_published(estate: Estate) -> None:
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first - marketing material links to it.")


def _staff(db: Session, request: Request, estate_id: int, permission: str = "estate.read"):
    estate = _estate_or_404(db, estate_id)
    access = require_estate_access(db, request, estate.organization_id, permission=permission)
    return estate, access


def _png(data: bytes, *, filename: str | None = None, public: bool = False) -> Response:
    headers = {"Cache-Control": "public, max-age=300"} if public else dict(NO_CACHE_PRIVATE)
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(data, media_type="image/png", headers=headers)


def _pdf(data: bytes, filename: str) -> Response:
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"', **NO_CACHE_PRIVATE})


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-") or "estate"


def _campaign_for_staff(db: Session, estate: Estate, campaign_id: int | None) -> EstateQrCampaign | None:
    if not campaign_id:
        return None
    campaign = db.get(EstateQrCampaign, campaign_id)
    if campaign is None or campaign.estate_id != estate.id:
        raise HTTPException(404, "Campaign not found")
    return campaign


# ── Rendering shared by staff, agent and public endpoints ───────────────────────────────────
def _render_flyer(ctx) -> bytes:
    return marketing_render.cached_render(("flyer", marketing_render.context_signature(ctx)), 60, lambda: marketing_pdf.render_flyer_pdf(ctx))


def _render_brochure(ctx) -> bytes:
    return marketing_render.cached_render(("brochure", marketing_render.context_signature(ctx)), 60, lambda: marketing_pdf.render_brochure_pdf(ctx))


def _render_estate_ad(ctx, fmt: str, *, qr: bool = True) -> bytes:
    try:
        return marketing_render.cached_render(("estate-ad", fmt, qr, marketing_render.context_signature(ctx)), 60, lambda: marketing_render.compose_estate_ad(ctx, fmt, qr=qr))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _render_plot_ad(ctx, plot: EstatePlot, fmt: str, *, qr: bool = True) -> bytes:
    key = ("plot-ad", fmt, qr, plot.id, plot.commercial_status, str(plot.asking_price), marketing_render.context_signature(ctx))
    return marketing_render.cached_render(key, 120, lambda: marketing_render.compose_plot_ad(ctx, plot, fmt, qr=qr))


def _ctx_plot(ctx, plot_id: int) -> EstatePlot:
    plot = next((item for item in ctx.plots if item.id == plot_id), None)
    if plot is None:
        raise HTTPException(404, "Plot not found")
    return plot


def _build_ctx(db: Session, estate: Estate, source: str | None):
    _require_published(estate)
    try:
        return marketing_render.build_context(db, estate, source=source)
    except Exception as exc:
        raise HTTPException(409, "Marketing material could not be prepared for this estate") from exc


# ═════════════════════════════════════════════════════════════════════════════════════════════
# PUBLIC
# ═════════════════════════════════════════════════════════════════════════════════════════════
@router.post("/public/{slug}/events", status_code=202)
def record_public_event(slug: str, payload: PublicEventCreate, request: Request, db: Session = Depends(get_db)):
    if throttled(client_ip(request), "public-events", limit=120, window_seconds=60):
        return {"status": "ignored"}
    estate = _public_estate(db, slug)
    plot_id = None
    if payload.plot_id:
        plot = db.query(EstatePlot.id).filter(EstatePlot.id == payload.plot_id, EstatePlot.estate_id == estate.id).first()
        plot_id = plot[0] if plot else None
    campaign = resolve_campaign(db, estate.id, payload.source)
    db.add(EstatePublicEvent(estate_id=estate.id, plot_id=plot_id, event_type=payload.event_type, source_code=campaign.code if campaign else None))
    db.commit()
    return {"status": "recorded"}


def _og_html(*, title: str, description: str, image: str, target: str) -> str:
    t, d, i, u = (html.escape(value, quote=True) for value in (title, description, image, target))
    script_target = json.dumps(target).replace("</", "<\\/")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{t}</title><meta name=\"description\" content=\"{d}\">"
        f"<meta property=\"og:type\" content=\"website\"><meta property=\"og:title\" content=\"{t}\">"
        f"<meta property=\"og:description\" content=\"{d}\"><meta property=\"og:image\" content=\"{i}\">"
        "<meta property=\"og:image:width\" content=\"1200\"><meta property=\"og:image:height\" content=\"630\">"
        f"<meta property=\"og:url\" content=\"{u}\"><meta name=\"twitter:card\" content=\"summary_large_image\">"
        f"<meta name=\"twitter:title\" content=\"{t}\"><meta name=\"twitter:image\" content=\"{i}\">"
        f"<noscript><meta http-equiv=\"refresh\" content=\"0;url={u}\"></noscript></head>"
        f"<body style=\"font-family:system-ui,sans-serif;padding:24px\"><p>Opening the live map&hellip; <a href=\"{u}\">Continue</a></p>"
        f"<script>window.location.replace({script_target});</script></body></html>"
    )


def _share_response(db: Session, slug: str, plot_id: int | None, source: str | None) -> HTMLResponse:
    estate = _public_estate(db, slug)
    campaign = resolve_campaign(db, estate.id, source)
    code = campaign.code if campaign else None
    query = f"?source={code}" if code else ""
    target = public_page_url(estate, source=code, plot_id=plot_id)
    if plot_id:
        plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved").one_or_none()
        if plot is None:
            raise HTTPException(404, "Plot not found")
        status = marketing_render.STATUS_LABEL.get(plot.commercial_status, "Not available")
        price = f" - {marketing_render.naira(plot.asking_price)}" if (estate.public_show_prices and plot.asking_price is not None) else ""
        title = f"Plot {plot.plot_number} at {estate.name}{price}"
        description = f"{marketing_render.area_text(plot.area_sqm)} - {status}. View it on the live satellite map and reserve online."
        image = f"{api_url()}/estates/public/{estate.public_slug}/plots/{plot.id}/card.png{query}"
    else:
        title = estate.name
        description = estate.public_tagline or f"Browse available plots at {estate.name} on the live map and reserve online."
        image = f"{api_url()}/estates/public/{estate.public_slug}/card.png{query}"
    return HTMLResponse(_og_html(title=title, description=description, image=image, target=target), headers={"Cache-Control": "public, max-age=300"})


@router.get("/public/{slug}/share")
def public_share_estate(slug: str, source: str | None = None, db: Session = Depends(get_db)):
    return _share_response(db, slug, None, source)


@router.get("/public/{slug}/share/plots/{plot_id}")
def public_share_plot(slug: str, plot_id: int, source: str | None = None, db: Session = Depends(get_db)):
    return _share_response(db, slug, plot_id, source)


@router.get("/public/{slug}/card.png")
def public_estate_card(slug: str, request: Request, source: str | None = None, db: Session = Depends(get_db)):
    if throttled(client_ip(request), "public-cards", limit=40, window_seconds=60):
        raise HTTPException(429, "Too many requests")
    estate = _public_estate(db, slug)
    ctx = _build_ctx(db, estate, source)
    return _png(_render_estate_ad(ctx, "landscape", qr=False), public=True)


@router.get("/public/{slug}/plots/{plot_id}/card.png")
def public_plot_card(slug: str, plot_id: int, request: Request, source: str | None = None, db: Session = Depends(get_db)):
    if throttled(client_ip(request), "public-cards", limit=40, window_seconds=60):
        raise HTTPException(429, "Too many requests")
    estate = _public_estate(db, slug)
    ctx = _build_ctx(db, estate, source)
    return _png(_render_plot_ad(ctx, _ctx_plot(ctx, plot_id), "landscape", qr=False), public=True)


# ── Inspections (public) ────────────────────────────────────────────────────────────────────
def _slot_taken(db: Session, slot_id: int) -> int:
    return int(db.query(func.coalesce(func.sum(EstateInspectionBooking.party_size), 0)).filter(
        EstateInspectionBooking.slot_id == slot_id,
        EstateInspectionBooking.status.in_(("booked", "attended")),
    ).scalar() or 0)


def _slot_payload(db: Session, slot: EstateInspectionSlot) -> dict:
    taken = _slot_taken(db, slot.id)
    return {
        "id": slot.id,
        "starts_at": slot.starts_at,
        "duration_minutes": slot.duration_minutes,
        "capacity": slot.capacity,
        "remaining": max(0, slot.capacity - taken),
        "note": slot.note,
        "status": slot.status,
    }


@router.get("/public/{slug}/inspection-slots")
def public_inspection_slots(slug: str, db: Session = Depends(get_db)):
    estate = _public_estate(db, slug)
    horizon = _now() + timedelta(hours=1)
    slots = db.query(EstateInspectionSlot).filter(
        EstateInspectionSlot.estate_id == estate.id,
        EstateInspectionSlot.status == "open",
        EstateInspectionSlot.starts_at > horizon,
    ).order_by(EstateInspectionSlot.starts_at.asc()).limit(24).all()
    return {"slots": [_slot_payload(db, slot) for slot in slots], "meeting_point": estate.public_meeting_point if isinstance(estate.public_meeting_point, dict) else None}


@router.post("/public/{slug}/inspection-bookings", status_code=201)
def public_book_inspection(slug: str, payload: InspectionBookingCreate, request: Request, db: Session = Depends(get_db)):
    if throttled(client_ip(request), "inspection-book", limit=6, window_seconds=3600):
        raise HTTPException(429, "Too many booking attempts. Please try again later.")
    estate = _public_estate(db, slug)
    phone_digits = normalize_phone_digits(payload.phone)
    if not phone_digits:
        raise HTTPException(422, "Enter a valid phone number")
    email = str(payload.email or "").strip().lower() or None
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(422, "Enter a valid email address or leave it blank")
    slot = db.query(EstateInspectionSlot).filter(EstateInspectionSlot.id == payload.slot_id, EstateInspectionSlot.estate_id == estate.id).with_for_update().one_or_none()
    if slot is None or slot.status != "open" or _aware(slot.starts_at) <= _now() + timedelta(hours=1):
        raise HTTPException(409, "This inspection time is no longer available")
    duplicates = db.query(EstateInspectionBooking).filter(EstateInspectionBooking.slot_id == slot.id, EstateInspectionBooking.status == "booked").all()
    if any(normalize_phone_digits(item.phone) == phone_digits for item in duplicates):
        raise HTTPException(409, "This phone number already has a booking for that time")
    remaining = slot.capacity - _slot_taken(db, slot.id)
    if payload.party_size > remaining:
        raise HTTPException(409, f"Only {max(remaining, 0)} place(s) left for that time" if remaining > 0 else "That inspection time is full")
    plot = None
    if payload.plot_id:
        plot = db.query(EstatePlot).filter(EstatePlot.id == payload.plot_id, EstatePlot.estate_id == estate.id).one_or_none()
    campaign = resolve_campaign(db, estate.id, payload.source)
    token = secrets.token_urlsafe(24)
    booking = EstateInspectionBooking(
        manage_token_hash=hash_portal_token(token),
        organization_id=estate.organization_id,
        estate_id=estate.id,
        slot_id=slot.id,
        plot_id=plot.id if plot else None,
        full_name=payload.full_name.strip(),
        phone=payload.phone.strip(),
        email=email,
        party_size=payload.party_size,
        note=payload.note.strip() if payload.note else None,
        source_code=campaign.code if campaign else None,
        source_channel=campaign.channel if campaign else None,
        assigned_agent_subject_type=campaign.assigned_agent_subject_type if campaign else None,
        assigned_agent_subject_id=campaign.assigned_agent_subject_id if campaign else None,
        status="booked",
    )
    db.add(booking)
    db.flush()
    append_estate_audit_event(
        db,
        organization_id=estate.organization_id,
        actor=None,
        action="inspection_booking.created",
        entity_type="estate_inspection_booking",
        entity_id=booking.id,
        after_data={"slot_id": slot.id, "party_size": booking.party_size, "plot_id": booking.plot_id, "source_code": booking.source_code},
        metadata={"source": "public_estate_page"},
    )
    db.commit()
    confirmation_sent = marketing_alerts.send_booking_confirmation(db, estate=estate, slot=slot, booking=booking, plot=plot, manage_token=token)
    marketing_alerts.notify_staff_of_booking(db, estate=estate, slot=slot, booking=booking, plot=plot)
    return {
        "status": "booked",
        "booking_id": booking.booking_uid,
        "manage_token": token,
        "confirmation_email_sent": confirmation_sent,
        "starts_at": slot.starts_at,
        "calendar_url": marketing_alerts.calendar_link(estate, slot),
        "directions_url": marketing_alerts.meeting_point_link(estate),
    }


def _booking_by_token(db: Session, token: str) -> EstateInspectionBooking:
    booking = db.query(EstateInspectionBooking).filter(EstateInspectionBooking.manage_token_hash == hash_portal_token(token)).one_or_none()
    if booking is None:
        raise HTTPException(404, "This booking link is invalid")
    return booking


@router.get("/public/inspection-bookings/{token}")
def public_get_booking(token: str, db: Session = Depends(get_db)):
    booking = _booking_by_token(db, token)
    slot = db.get(EstateInspectionSlot, booking.slot_id)
    estate = db.get(Estate, booking.estate_id)
    plot = db.get(EstatePlot, booking.plot_id) if booking.plot_id else None
    return {
        "estate_name": estate.name if estate else None,
        "estate_slug": estate.public_slug if estate else None,
        "full_name": booking.full_name,
        "party_size": booking.party_size,
        "plot_number": plot.plot_number if plot else None,
        "status": booking.status,
        "starts_at": slot.starts_at if slot else None,
        "duration_minutes": slot.duration_minutes if slot else None,
        "slot_status": slot.status if slot else None,
        "meeting_point": estate.public_meeting_point if estate and isinstance(estate.public_meeting_point, dict) else None,
        "directions_url": marketing_alerts.meeting_point_link(estate) if estate else None,
        "calendar_url": marketing_alerts.calendar_link(estate, slot) if estate and slot else None,
        "can_cancel": booking.status == "booked" and slot is not None and _aware(slot.starts_at) > _now(),
    }


@router.post("/public/inspection-bookings/{token}/cancel")
def public_cancel_booking(token: str, db: Session = Depends(get_db)):
    booking = _booking_by_token(db, token)
    slot = db.get(EstateInspectionSlot, booking.slot_id)
    if booking.status != "booked" or slot is None or _aware(slot.starts_at) <= _now():
        raise HTTPException(409, "This booking can no longer be cancelled")
    booking.status = "cancelled"
    append_estate_audit_event(db, organization_id=booking.organization_id, actor=None, action="inspection_booking.cancelled", entity_type="estate_inspection_booking", entity_id=booking.id, metadata={"by": "visitor"})
    db.commit()
    return {"status": "cancelled"}


# ── Progress feed (public) ──────────────────────────────────────────────────────────────────
def _progress_payload(db: Session, row: EstateProgressUpdate, estate: Estate, *, public: bool) -> dict:
    plot = db.get(EstatePlot, row.plot_id) if row.plot_id else None
    media_type = "none"
    media_url = None
    if row.media_object_key:
        media_type = "video" if str(row.media_mime or "").startswith("video") else "image"
        media_url = f"/estates/public/{estate.public_slug}/progress/{row.update_uid}/media" if public else f"/estates/{estate.id}/marketing/progress/{row.id}/media"
    elif row.media_url:
        media_type = "link"
        media_url = row.media_url
    payload = {
        "id": row.update_uid if public else row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "plot_number": plot.plot_number if plot else None,
        "media_type": media_type,
        "media_url": media_url,
        "captured_at": row.captured_at or row.created_at,
        "verification": row.verification,
        "verification_label": marketing_field.VERIFICATION_LABELS.get(row.verification, "Location not verified"),
        "location_source": row.location_source,
        "created_at": row.created_at,
    }
    if not public:
        payload.update({"is_published": row.is_published, "plot_id": row.plot_id, "distance_m": row.distance_m, "latitude": row.latitude, "longitude": row.longitude})
    return payload


@router.get("/public/{slug}/progress")
def public_progress_feed(slug: str, db: Session = Depends(get_db)):
    estate = _public_estate(db, slug)
    rows = db.query(EstateProgressUpdate).filter(EstateProgressUpdate.estate_id == estate.id, EstateProgressUpdate.is_published.is_(True)).order_by(EstateProgressUpdate.created_at.desc()).limit(30).all()
    return {"updates": [_progress_payload(db, row, estate, public=True) for row in rows]}


@router.get("/public/{slug}/progress/{update_uid}/media")
def public_progress_media(slug: str, update_uid: str, db: Session = Depends(get_db)):
    estate = _public_estate(db, slug)
    row = db.query(EstateProgressUpdate).filter(EstateProgressUpdate.update_uid == update_uid, EstateProgressUpdate.estate_id == estate.id, EstateProgressUpdate.is_published.is_(True)).one_or_none()
    if row is None or not row.media_object_key:
        raise HTTPException(404, "Media not found")
    data, mime = marketing_field.read_progress_media(row.media_object_key)
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


# ═════════════════════════════════════════════════════════════════════════════════════════════
# STAFF - materials, share links, overview
# ═════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/{estate_id}/marketing/materials/flyer.pdf")
def staff_flyer(estate_id: int, request: Request, campaign_id: int | None = None, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    campaign = _campaign_for_staff(db, estate, campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    return _pdf(_render_flyer(ctx), f"{_safe_name(estate.name)}-flyer.pdf")


@router.get("/{estate_id}/marketing/materials/brochure.pdf")
def staff_brochure(estate_id: int, request: Request, campaign_id: int | None = None, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    campaign = _campaign_for_staff(db, estate, campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    return _pdf(_render_brochure(ctx), f"{_safe_name(estate.name)}-brochure.pdf")


@router.get("/{estate_id}/marketing/materials/ad.png")
def staff_estate_ad(estate_id: int, request: Request, format: str = "post", campaign_id: int | None = None, db: Session = Depends(get_db)):
    fmt = _ad_format(format)
    estate, _access = _staff(db, request, estate_id)
    campaign = _campaign_for_staff(db, estate, campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    return _png(_render_estate_ad(ctx, fmt), filename=f"{_safe_name(estate.name)}-{fmt}.png")


@router.get("/{estate_id}/marketing/materials/plots/{plot_id}/ad.png")
def staff_plot_ad(estate_id: int, plot_id: int, request: Request, format: str = "post", campaign_id: int | None = None, db: Session = Depends(get_db)):
    fmt = _ad_format(format)
    estate, _access = _staff(db, request, estate_id)
    campaign = _campaign_for_staff(db, estate, campaign_id)
    ctx = _build_ctx(db, estate, campaign.code if campaign else None)
    plot = _ctx_plot(ctx, plot_id)
    return _png(_render_plot_ad(ctx, plot, fmt), filename=f"{_safe_name(estate.name)}-plot-{_safe_name(plot.plot_number)}-{fmt}.png")


def _share_links_payload(db: Session, estate: Estate, campaign: EstateQrCampaign | None, agent_name: str | None, organization_name: str) -> dict:
    code = campaign.code if campaign else None
    plots = db.query(EstatePlot).filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved", EstatePlot.commercial_status == "available").all()
    plots.sort(key=lambda plot: marketing_render._natural_key(plot.plot_number))
    sender = agent_name or organization_name
    estate_text = f"{estate.name} - verified plots available now. See the live map, prices and reserve online: "
    return {
        "page_url": public_page_url(estate, source=code),
        "share_url": share_page_url(estate, source=code),
        "whatsapp_text": f"{estate_text}{share_page_url(estate, source=code)}",
        "sender": sender,
        "plots": [
            {
                "id": plot.id,
                "plot_number": plot.plot_number,
                "area_sqm": float(plot.area_sqm) if plot.area_sqm is not None else None,
                "price": str(plot.asking_price) if (estate.public_show_prices and plot.asking_price is not None) else None,
                "page_url": public_page_url(estate, source=code, plot_id=plot.id),
                "share_url": share_page_url(estate, source=code, plot_id=plot.id),
            }
            for plot in plots[:400]
        ],
    }


@router.get("/{estate_id}/marketing/share-links")
def staff_share_links(estate_id: int, request: Request, campaign_id: int | None = None, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id)
    _require_published(estate)
    campaign = _campaign_for_staff(db, estate, campaign_id)
    return _share_links_payload(db, estate, campaign, None, access.organization_name)


def _period_start(days: int) -> datetime:
    return _now() - timedelta(days=max(1, min(int(days), 365)))


def _lead_age_hours(row: EstatePublicReservationRequest) -> int:
    return int((_now() - (_aware(row.created_at) or _now())).total_seconds() // 3600)


def _followup_item(db: Session, row: EstatePublicReservationRequest, *, estate_name: str | None = None) -> dict:
    plot = db.get(EstatePlot, row.plot_id)
    estate = db.get(Estate, row.estate_id) if estate_name is None else None
    name = estate_name or (estate.name if estate else "Estate")
    campaign = resolve_campaign(db, row.estate_id, row.source_code)
    member = agent_member_for(db, campaign)
    agent = member.subject_id if member else None
    return {
        "id": row.id,
        "name": row.full_name,
        "phone": row.phone,
        "email": row.email,
        "status": row.status,
        "estate_name": name,
        "plot": plot.plot_number if plot else None,
        "age_hours": _lead_age_hours(row),
        "agent": agent,
        "source_code": row.source_code,
        "whatsapp_url": marketing_alerts._lead_whatsapp(row.phone, row.full_name, name, plot.plot_number if plot else "-", agent),
        "created_at": row.created_at,
    }


@router.get("/{estate_id}/marketing/overview")
def staff_marketing_overview(estate_id: int, request: Request, days: int = 30, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    since = _period_start(days)
    campaigns = db.query(EstateQrCampaign).filter(EstateQrCampaign.estate_id == estate.id).order_by(EstateQrCampaign.created_at.desc()).all()

    lead_rows = db.query(EstatePublicReservationRequest).filter(EstatePublicReservationRequest.estate_id == estate.id, EstatePublicReservationRequest.created_at >= since).all()
    event_rows = db.query(EstatePublicEvent.source_code, EstatePublicEvent.event_type, func.count(EstatePublicEvent.id)).filter(EstatePublicEvent.estate_id == estate.id, EstatePublicEvent.created_at >= since).group_by(EstatePublicEvent.source_code, EstatePublicEvent.event_type).all()
    booking_rows = db.query(EstateInspectionBooking).filter(EstateInspectionBooking.estate_id == estate.id, EstateInspectionBooking.created_at >= since, EstateInspectionBooking.status != "cancelled").all()
    allocation_rows = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate.id, EstateAllocation.created_at >= since, EstateAllocation.status.in_(("reserved", "allocated"))).all()

    def blank():
        return {"leads": 0, "whatsapp": 0, "shares": 0, "directions": 0, "inspections": 0, "reservations": 0, "sales": 0}

    by_code: dict[str | None, dict] = {}
    for row in lead_rows:
        by_code.setdefault(row.source_code, blank())["leads"] += 1
    for code, event_type, count in event_rows:
        bucket = by_code.setdefault(code, blank())
        key = {"whatsapp_click": "whatsapp", "share_click": "shares", "directions_click": "directions"}.get(event_type)
        if key:
            bucket[key] += int(count)
    for row in booking_rows:
        by_code.setdefault(row.source_code, blank())["inspections"] += 1
    for row in allocation_rows:
        bucket = by_code.setdefault(row.lead_source_code, blank())
        bucket["reservations"] += 1
        if row.status == "allocated":
            bucket["sales"] += 1

    channel_rows = []
    for campaign in campaigns:
        stats = by_code.get(campaign.code, blank())
        agent = _agent_display_name(db, organization_id=estate.organization_id, subject_type=campaign.assigned_agent_subject_type, subject_id=campaign.assigned_agent_subject_id) if campaign.assigned_agent_subject_id else None
        channel_rows.append({"id": campaign.id, "code": campaign.code, "name": campaign.name, "channel": campaign.channel, "agent": agent, "link_opens": int(campaign.scan_count or 0), **stats})
    direct = by_code.get(None, blank())

    total = blank()
    for stats in by_code.values():
        for key, value in stats.items():
            total[key] += value

    followups = db.query(EstatePublicReservationRequest).filter(
        EstatePublicReservationRequest.estate_id == estate.id,
        EstatePublicReservationRequest.status.in_(("new", "contacted")),
    ).order_by(EstatePublicReservationRequest.created_at.asc()).limit(60).all()
    queue = []
    for row in followups:
        age = _lead_age_hours(row)
        if row.status == "new" and age >= 1 or row.status == "contacted" and age >= 72:
            queue.append(_followup_item(db, row, estate_name=estate.name))
    upcoming = db.query(EstateInspectionSlot).filter(EstateInspectionSlot.estate_id == estate.id, EstateInspectionSlot.status == "open", EstateInspectionSlot.starts_at > _now()).count()
    return {
        "period_days": days,
        "totals": {**total, "link_opens": sum(int(c.scan_count or 0) for c in campaigns), "upcoming_inspections": upcoming},
        "channels": channel_rows,
        "direct": direct,
        "follow_up_queue": queue[:15],
        "follow_up_count": len(queue),
        "published": bool(estate.public_enabled and estate.public_slug),
    }


# ── Agent links & leaderboard (staff) ───────────────────────────────────────────────────────
def _agent_members(db: Session, organization_id: int) -> list[EstateOrganizationMember]:
    return db.query(EstateOrganizationMember).filter(
        EstateOrganizationMember.organization_id == organization_id,
        EstateOrganizationMember.is_active.is_(True),
        EstateOrganizationMember.role_key.in_(("sales", "marketer")),
    ).order_by(EstateOrganizationMember.subject_id.asc()).all()


def compute_leaderboard(db: Session, organization_id: int, days: int) -> list[dict]:
    since = _period_start(days)
    rows: dict[tuple[str, str], dict] = {}

    def entry(subject_type: str, subject_id: str) -> dict:
        key = (subject_type, subject_id)
        if key not in rows:
            rows[key] = {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "name": _agent_display_name(db, organization_id=organization_id, subject_type=subject_type, subject_id=subject_id),
                "leads": 0, "whatsapp_clicks": 0, "inspections_booked": 0, "inspections_attended": 0,
                "reservations": 0, "sales": 0, "sales_value": Decimal("0"), "commission_earned": Decimal("0"), "commission_paid": Decimal("0"),
            }
        return rows[key]

    for member in _agent_members(db, organization_id):
        entry(member.subject_type, member.subject_id)

    campaigns = db.query(EstateQrCampaign).filter(EstateQrCampaign.organization_id == organization_id).all()
    campaign_agent = {(c.estate_id, c.code): (c.assigned_agent_subject_type, c.assigned_agent_subject_id) for c in campaigns if c.assigned_agent_subject_id}
    estate_ids = [e[0] for e in db.query(Estate.id).filter(Estate.organization_id == organization_id).all()]

    for lead in db.query(EstatePublicReservationRequest).filter(EstatePublicReservationRequest.organization_id == organization_id, EstatePublicReservationRequest.created_at >= since, EstatePublicReservationRequest.assigned_agent_subject_id.isnot(None)).all():
        entry(lead.assigned_agent_subject_type, lead.assigned_agent_subject_id)["leads"] += 1
    for booking in db.query(EstateInspectionBooking).filter(EstateInspectionBooking.organization_id == organization_id, EstateInspectionBooking.created_at >= since, EstateInspectionBooking.assigned_agent_subject_id.isnot(None), EstateInspectionBooking.status != "cancelled").all():
        bucket = entry(booking.assigned_agent_subject_type, booking.assigned_agent_subject_id)
        bucket["inspections_booked"] += 1
        if booking.status == "attended":
            bucket["inspections_attended"] += 1
    if estate_ids:
        for estate_id, code, count in db.query(EstatePublicEvent.estate_id, EstatePublicEvent.source_code, func.count(EstatePublicEvent.id)).filter(EstatePublicEvent.estate_id.in_(estate_ids), EstatePublicEvent.created_at >= since, EstatePublicEvent.event_type == "whatsapp_click", EstatePublicEvent.source_code.isnot(None)).group_by(EstatePublicEvent.estate_id, EstatePublicEvent.source_code).all():
            agent = campaign_agent.get((estate_id, code))
            if agent:
                entry(*agent)["whatsapp_clicks"] += int(count)
    for allocation in db.query(EstateAllocation).filter(EstateAllocation.organization_id == organization_id, EstateAllocation.created_at >= since, EstateAllocation.sales_agent_subject_id.isnot(None), EstateAllocation.status.in_(("reserved", "allocated"))).all():
        bucket = entry(allocation.sales_agent_subject_type, allocation.sales_agent_subject_id)
        bucket["reservations"] += 1
        if allocation.status == "allocated":
            bucket["sales"] += 1
            bucket["sales_value"] += Decimal(str(allocation.agreed_price or 0))
            bucket["commission_earned"] += Decimal(str(allocation.commission_amount or 0))
    for bucket in rows.values():
        paid = db.query(func.coalesce(func.sum(EstateCommissionPayout.amount), 0)).filter(
            EstateCommissionPayout.organization_id == organization_id,
            EstateCommissionPayout.sales_agent_subject_type == bucket["subject_type"],
            EstateCommissionPayout.sales_agent_subject_id == bucket["subject_id"],
            EstateCommissionPayout.created_at >= since,
        ).scalar()
        bucket["commission_paid"] = Decimal(str(paid or 0))

    ordered = sorted(rows.values(), key=lambda item: (item["sales_value"], item["sales"], item["reservations"], item["inspections_attended"], item["leads"]), reverse=True)
    for index, item in enumerate(ordered, start=1):
        item["rank"] = index
    return ordered


@router.get("/organizations/{organization_id}/marketing/leaderboard")
def staff_leaderboard(organization_id: int, request: Request, days: int = 30, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="estate.read")
    can_see_money = has_permission(access.role_key, "estate.manage")
    board = compute_leaderboard(db, organization_id, days)
    own = (access.principal.subject_type, access.principal.subject_id)
    rows = []
    for item in board:
        is_you = (item["subject_type"], item["subject_id"]) == own
        row = {key: value for key, value in item.items() if key not in {"sales_value", "commission_earned", "commission_paid", "subject_type", "subject_id"}}
        row["is_you"] = is_you
        if can_see_money or is_you:
            row.update({"sales_value": str(item["sales_value"]), "commission_earned": str(item["commission_earned"]), "commission_paid": str(item["commission_paid"])})
        rows.append(row)
    return {"period_days": days, "rows": rows, "money_visible": can_see_money}


@router.post("/{estate_id}/marketing/agent-links")
def staff_ensure_agent_links(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission="estate.manage")
    _require_published(estate)
    links = []
    for member in _agent_members(db, estate.organization_id):
        campaign = ensure_agent_campaign(db, estate=estate, member=member)
        links.append({
            "campaign_id": campaign.id,
            "agent": member.subject_id,
            "email": member.contact_email,
            "phone": member.contact_phone,
            "page_url": public_page_url(estate, source=campaign.code),
            "share_url": share_page_url(estate, source=campaign.code),
            "link_opens": int(campaign.scan_count or 0),
        })
    db.commit()
    return {"links": links}


# ═════════════════════════════════════════════════════════════════════════════════════════════
# STAFF - inspections
# ═════════════════════════════════════════════════════════════════════════════════════════════
def _booking_payload(db: Session, booking: EstateInspectionBooking, slot: EstateInspectionSlot | None = None) -> dict:
    slot = slot or db.get(EstateInspectionSlot, booking.slot_id)
    plot = db.get(EstatePlot, booking.plot_id) if booking.plot_id else None
    agent = None
    if booking.assigned_agent_subject_id:
        agent = _agent_display_name(db, organization_id=booking.organization_id, subject_type=booking.assigned_agent_subject_type, subject_id=booking.assigned_agent_subject_id)
    estate = db.get(Estate, booking.estate_id)
    return {
        "id": booking.id,
        "slot_id": booking.slot_id,
        "starts_at": slot.starts_at if slot else None,
        "full_name": booking.full_name,
        "phone": booking.phone,
        "email": booking.email,
        "party_size": booking.party_size,
        "plot_number": plot.plot_number if plot else None,
        "note": booking.note,
        "status": booking.status,
        "staff_notes": booking.staff_notes,
        "source_code": booking.source_code,
        "agent": agent,
        "whatsapp_url": whatsapp_link(booking.phone, f"Hello {booking.full_name.split(' ')[0]}, this is a reminder about your site inspection at {estate.name if estate else 'the estate'}."),
        "created_at": booking.created_at,
    }


@router.get("/{estate_id}/marketing/inspection-slots")
def staff_list_slots(estate_id: int, request: Request, include_past: bool = False, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    query = db.query(EstateInspectionSlot).filter(EstateInspectionSlot.estate_id == estate.id)
    if not include_past:
        query = query.filter(EstateInspectionSlot.starts_at > _now() - timedelta(hours=6))
    slots = query.order_by(EstateInspectionSlot.starts_at.asc()).limit(80).all()
    return [_slot_payload(db, slot) for slot in slots]


@router.post("/{estate_id}/marketing/inspection-slots", status_code=201)
def staff_create_slots(estate_id: int, payload: InspectionSlotCreate, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission="estate.manage")
    first = _aware(payload.starts_at)
    if first <= _now():
        raise HTTPException(422, "Choose a time in the future")
    created = []
    for week in range(payload.repeat_weekly + 1):
        slot = EstateInspectionSlot(
            organization_id=estate.organization_id,
            estate_id=estate.id,
            starts_at=first + timedelta(weeks=week),
            duration_minutes=payload.duration_minutes,
            capacity=payload.capacity,
            note=payload.note.strip() if payload.note else None,
            status="open",
            created_by_subject_type=access.principal.subject_type,
            created_by_subject_id=access.principal.subject_id,
        )
        db.add(slot)
        created.append(slot)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="inspection_slot.created", entity_type="estate_inspection_slot", entity_id=created[0].id, after_data={"count": len(created), "starts_at": first.isoformat(), "capacity": payload.capacity})
    db.commit()
    return [_slot_payload(db, slot) for slot in created]


@router.patch("/marketing/inspection-slots/{slot_id}")
def staff_update_slot(slot_id: int, payload: InspectionSlotUpdate, request: Request, db: Session = Depends(get_db)):
    slot = db.get(EstateInspectionSlot, slot_id)
    if slot is None:
        raise HTTPException(404, "Inspection time not found")
    access = require_estate_access(db, request, slot.organization_id, permission="estate.manage")
    values = payload.model_dump(exclude_unset=True)
    was_cancelled = slot.status == "cancelled"
    if "capacity" in values and values["capacity"] is not None:
        if values["capacity"] < _slot_taken(db, slot.id):
            raise HTTPException(409, "Capacity cannot be lower than the places already booked")
        slot.capacity = values["capacity"]
    if "note" in values:
        slot.note = values["note"].strip() if values["note"] else None
    cancelled_bookings: list[EstateInspectionBooking] = []
    if "status" in values and values["status"]:
        slot.status = values["status"]
        if slot.status == "cancelled" and not was_cancelled:
            cancelled_bookings = db.query(EstateInspectionBooking).filter(EstateInspectionBooking.slot_id == slot.id, EstateInspectionBooking.status == "booked").all()
            for booking in cancelled_bookings:
                booking.status = "cancelled"
    append_estate_audit_event(db, organization_id=slot.organization_id, actor=access.principal, action="inspection_slot.updated", entity_type="estate_inspection_slot", entity_id=slot.id, after_data=values)
    db.commit()
    if cancelled_bookings:
        estate = db.get(Estate, slot.estate_id)
        marketing_alerts.send_slot_cancellation(db, estate=estate, slot=slot, bookings=cancelled_bookings)
    return _slot_payload(db, slot)


@router.get("/{estate_id}/marketing/inspection-bookings")
def staff_list_bookings(estate_id: int, request: Request, upcoming: bool = True, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    query = db.query(EstateInspectionBooking, EstateInspectionSlot).join(EstateInspectionSlot, EstateInspectionSlot.id == EstateInspectionBooking.slot_id).filter(EstateInspectionBooking.estate_id == estate.id)
    if upcoming:
        query = query.filter(EstateInspectionSlot.starts_at > _now() - timedelta(hours=12))
    rows = query.order_by(EstateInspectionSlot.starts_at.asc(), EstateInspectionBooking.created_at.asc()).limit(300).all()
    return [_booking_payload(db, booking, slot) for booking, slot in rows]


@router.patch("/marketing/inspection-bookings/{booking_id}")
def staff_update_booking(booking_id: int, payload: InspectionBookingUpdate, request: Request, db: Session = Depends(get_db)):
    booking = db.get(EstateInspectionBooking, booking_id)
    if booking is None:
        raise HTTPException(404, "Booking not found")
    access = require_estate_access(db, request, booking.organization_id, permission="estate.read")
    is_manager = has_permission(access.role_key, "estate.manage")
    is_own = booking.assigned_agent_subject_type == access.principal.subject_type and booking.assigned_agent_subject_id == access.principal.subject_id
    if not (is_manager or is_own or has_permission(access.role_key, "field.manage")):
        raise HTTPException(403, "You do not have permission to update this booking")
    values = payload.model_dump(exclude_unset=True)
    if values.get("status"):
        booking.status = values["status"]
    if "staff_notes" in values:
        booking.staff_notes = values["staff_notes"].strip() if values["staff_notes"] else None
    append_estate_audit_event(db, organization_id=booking.organization_id, actor=access.principal, action="inspection_booking.updated", entity_type="estate_inspection_booking", entity_id=booking.id, after_data=values)
    db.commit()
    return _booking_payload(db, booking)


# ═════════════════════════════════════════════════════════════════════════════════════════════
# STAFF - progress feed
# ═════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/{estate_id}/marketing/progress")
def staff_list_progress(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    rows = db.query(EstateProgressUpdate).filter(EstateProgressUpdate.estate_id == estate.id).order_by(EstateProgressUpdate.created_at.desc()).limit(100).all()
    return [_progress_payload(db, row, estate, public=False) for row in rows]


@router.post("/{estate_id}/marketing/progress", status_code=201)
def staff_create_progress(
    estate_id: int,
    request: Request,
    title: str = Form(...),
    kind: str = Form("photo"),
    body: str | None = Form(None),
    plot_id: int | None = Form(None),
    video_url: str | None = Form(None),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    accuracy_m: float | None = Form(None),
    captured_at: str | None = Form(None),
    is_published: bool = Form(True),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    estate, access = _staff(db, request, estate_id, permission="field.manage")
    clean_kind = str(kind or "photo").strip().lower()
    if clean_kind not in {"photo", "video", "drone", "milestone", "note"}:
        raise HTTPException(422, "Choose photo, video, drone, milestone or note")
    clean_title = str(title or "").strip()
    if len(clean_title) < 2:
        raise HTTPException(422, "Give this update a short title")
    plot = None
    if plot_id:
        plot = db.query(EstatePlot).filter(EstatePlot.id == plot_id, EstatePlot.estate_id == estate.id).one_or_none()
        if plot is None:
            raise HTTPException(404, "Plot not found")
    link = str(video_url or "").strip() or None
    if link and not link.lower().startswith(("https://", "http://")):
        raise HTTPException(422, "The video link must start with https://")

    media_key = None
    media_mime = None
    exif: dict = {}
    if file is not None and file.filename:
        limit = marketing_field.MAX_VIDEO_BYTES
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, "That file is too large")
        media_type, mime = marketing_field.detect_media(data)
        if media_type == "image":
            if len(data) > marketing_field.MAX_PHOTO_BYTES:
                raise HTTPException(413, "That photo is too large")
            exif = marketing_field.extract_exif(data)
            data = marketing_field.process_photo(data)
            mime = "image/jpeg"
        if clean_kind in {"photo"} and media_type == "video":
            clean_kind = "video"
        if clean_kind == "video" and media_type == "image":
            clean_kind = "photo"
        organization = db.get(EstateOrganization, estate.organization_id)
        media_key = marketing_field.store_progress_media(organization_uid=organization.organization_uid, estate_uid=estate.estate_uid, mime=mime, data=data)
        media_mime = mime
    elif clean_kind in {"photo", "video"} and not link:
        raise HTTPException(422, "Attach a photo or video, or paste a video link")

    lat = exif.get("lat", latitude)
    lon = exif.get("lon", longitude)
    location_source = "exif" if "lat" in exif else ("device" if latitude is not None and longitude is not None else "none")
    verification, distance = "unverified", None
    if lat is not None and lon is not None:
        approved = db.query(EstatePlot).filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved").all()
        verification, distance = marketing_field.verify_location(lat=float(lat), lon=float(lon), accuracy_m=accuracy_m, plots=approved, boundary=estate.boundary, focus_plot=plot)
    captured = exif.get("captured_at")
    if captured is None and captured_at:
        try:
            captured = _aware(datetime.fromisoformat(captured_at.replace("Z", "+00:00")))
        except ValueError:
            captured = None
    row = EstateProgressUpdate(
        organization_id=estate.organization_id,
        estate_id=estate.id,
        plot_id=plot.id if plot else None,
        kind=clean_kind,
        title=clean_title[:255],
        body=str(body or "").strip() or None,
        media_object_key=media_key,
        media_mime=media_mime,
        media_url=link,
        captured_at=captured or _now(),
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        location_accuracy_m=accuracy_m,
        location_source=location_source,
        verification=verification,
        distance_m=round(distance, 1) if distance is not None else None,
        is_published=bool(is_published),
        posted_by_subject_type=access.principal.subject_type,
        posted_by_subject_id=access.principal.subject_id,
    )
    db.add(row)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="progress_update.created", entity_type="estate_progress_update", entity_id=row.id, after_data={"kind": row.kind, "verification": row.verification, "plot_id": row.plot_id, "published": row.is_published})
    db.commit()
    return _progress_payload(db, row, estate, public=False)


@router.patch("/marketing/progress/{update_id}")
def staff_update_progress(update_id: int, payload: ProgressUpdatePatch, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateProgressUpdate, update_id)
    if row is None:
        raise HTTPException(404, "Update not found")
    access = require_estate_access(db, request, row.organization_id, permission="field.manage")
    values = payload.model_dump(exclude_unset=True)
    if values.get("title"):
        row.title = values["title"].strip()
    if "body" in values:
        row.body = values["body"].strip() if values["body"] else None
    if "is_published" in values and values["is_published"] is not None:
        row.is_published = values["is_published"]
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="progress_update.updated", entity_type="estate_progress_update", entity_id=row.id, after_data=values)
    db.commit()
    return _progress_payload(db, row, db.get(Estate, row.estate_id), public=False)


@router.delete("/marketing/progress/{update_id}", status_code=204)
def staff_delete_progress(update_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateProgressUpdate, update_id)
    if row is None:
        raise HTTPException(404, "Update not found")
    access = require_estate_access(db, request, row.organization_id, permission="field.manage")
    append_estate_audit_event(db, organization_id=row.organization_id, actor=access.principal, action="progress_update.deleted", entity_type="estate_progress_update", entity_id=row.id, before_data={"title": row.title})
    key = row.media_object_key
    db.delete(row)
    db.commit()
    if key:
        try:
            settings = build_r2_settings(prefix="R2")
            if settings:
                delete_object_best_effort(settings, key)
        except Exception:
            pass
    return Response(status_code=204)


@router.get("/{estate_id}/marketing/progress/{update_id}/media")
def staff_progress_media(estate_id: int, update_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    row = db.get(EstateProgressUpdate, update_id)
    if row is None or row.estate_id != estate.id or not row.media_object_key:
        raise HTTPException(404, "Media not found")
    data, mime = marketing_field.read_progress_media(row.media_object_key)
    return Response(data, media_type=mime, headers=NO_CACHE_PRIVATE)


# ═════════════════════════════════════════════════════════════════════════════════════════════
# AGENT PORTAL (token) - kit, materials, follow-up
# ═════════════════════════════════════════════════════════════════════════════════════════════
def _agent_estate(db: Session, organization_id: int, estate_id: int) -> Estate:
    estate = db.get(Estate, estate_id)
    if estate is None or estate.organization_id != organization_id or estate.archived_at is not None:
        raise HTTPException(404, "Estate not found")
    return estate


def _agent_ctx(db: Session, member: EstateOrganizationMember, estate: Estate):
    _require_published(estate)
    campaign = ensure_agent_campaign(db, estate=estate, member=member)
    db.flush()
    return marketing_render.build_context(db, estate, source=campaign.code), campaign


@router.get("/agent-portal/{token}/kit")
def agent_kit(token: str, days: int = 30, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    estates = db.query(Estate).filter(Estate.organization_id == organization.id, Estate.archived_at.is_(None), Estate.status != "archived").order_by(Estate.name.asc()).all()
    agent_name = member.subject_id
    kit_estates = []
    for estate in estates:
        published = bool(estate.public_enabled and estate.public_slug)
        item = {"id": estate.id, "name": estate.name, "published": published}
        if published:
            campaign = ensure_agent_campaign(db, estate=estate, member=member)
            share = _share_links_payload(db, estate, campaign, agent_name, organization.name)
            counts: dict[str, int] = {}
            for status, count in db.query(EstatePlot.commercial_status, func.count(EstatePlot.id)).filter(EstatePlot.estate_id == estate.id, EstatePlot.geometry_status == "approved").group_by(EstatePlot.commercial_status).all():
                counts[status] = int(count)
            item.update({
                "campaign": {"id": campaign.id, "code": campaign.code, "link_opens": int(campaign.scan_count or 0)},
                "page_url": share["page_url"],
                "share_url": share["share_url"],
                "whatsapp_text": share["whatsapp_text"],
                "plots": share["plots"],
                "available": counts.get("available", 0),
                "reserved": counts.get("reserved", 0),
                "min_price": min((float(p["price"]) for p in share["plots"] if p["price"]), default=None),
                "materials": {
                    "flyer": f"/estates/agent-portal/{token}/marketing/{estate.id}/flyer.pdf",
                    "brochure": f"/estates/agent-portal/{token}/marketing/{estate.id}/brochure.pdf",
                    "ad": f"/estates/agent-portal/{token}/marketing/{estate.id}/ad.png",
                    "plot_ad": f"/estates/agent-portal/{token}/marketing/{estate.id}/plots/{{plot_id}}/ad.png",
                },
            })
        kit_estates.append(item)
    db.flush()

    lead_rows = db.query(EstatePublicReservationRequest).filter(
        EstatePublicReservationRequest.organization_id == organization.id,
        EstatePublicReservationRequest.assigned_agent_subject_type == member.subject_type,
        EstatePublicReservationRequest.assigned_agent_subject_id == member.subject_id,
        EstatePublicReservationRequest.status.in_(("new", "contacted")),
    ).order_by(EstatePublicReservationRequest.created_at.asc()).limit(100).all()
    followups = [_followup_item(db, row) for row in lead_rows]

    booking_rows = db.query(EstateInspectionBooking, EstateInspectionSlot).join(EstateInspectionSlot, EstateInspectionSlot.id == EstateInspectionBooking.slot_id).filter(
        EstateInspectionBooking.organization_id == organization.id,
        EstateInspectionBooking.assigned_agent_subject_type == member.subject_type,
        EstateInspectionBooking.assigned_agent_subject_id == member.subject_id,
        EstateInspectionBooking.status.in_(("booked", "attended", "no_show")),
        EstateInspectionSlot.starts_at > _now() - timedelta(days=7),
    ).order_by(EstateInspectionSlot.starts_at.asc()).limit(60).all()
    inspections = [_booking_payload(db, booking, slot) for booking, slot in booking_rows]

    tiers = commissions._tier_dicts(commissions.get_commission_tiers(db, organization.id))
    volume = commissions.cumulative_sales_before(db, organization_id=organization.id, subject_type=member.subject_type, subject_id=member.subject_id)
    current = commissions.resolve_tier(tiers, volume)
    ordered = sorted(tiers, key=lambda tier: tier["min_cumulative_sales"])
    upcoming = next((tier for tier in ordered if tier["min_cumulative_sales"] > volume), None)
    earned = db.query(func.coalesce(func.sum(EstateAllocation.commission_amount), 0)).filter(EstateAllocation.organization_id == organization.id, EstateAllocation.sales_agent_subject_type == member.subject_type, EstateAllocation.sales_agent_subject_id == member.subject_id, EstateAllocation.status == "allocated").scalar()
    paid = db.query(func.coalesce(func.sum(EstateCommissionPayout.amount), 0)).filter(EstateCommissionPayout.organization_id == organization.id, EstateCommissionPayout.sales_agent_subject_type == member.subject_type, EstateCommissionPayout.sales_agent_subject_id == member.subject_id).scalar()
    pipeline = db.query(func.coalesce(func.sum(EstateAllocation.agreed_price), 0)).filter(EstateAllocation.organization_id == organization.id, EstateAllocation.sales_agent_subject_type == member.subject_type, EstateAllocation.sales_agent_subject_id == member.subject_id, EstateAllocation.status == "reserved").scalar()
    commission = {
        "earned": str(Decimal(str(earned or 0))),
        "paid": str(Decimal(str(paid or 0))),
        "due": str(max(Decimal("0"), Decimal(str(earned or 0)) - Decimal(str(paid or 0)))),
        "sales_volume": str(volume),
        "tier": {"label": current["label"], "rate_percent": str(current["rate_percent"])},
        "next_tier": {"label": upcoming["label"], "rate_percent": str(upcoming["rate_percent"]), "needed": str(upcoming["min_cumulative_sales"] - volume)} if upcoming else None,
        "pipeline_value": str(Decimal(str(pipeline or 0))),
        "pipeline_estimate": str((Decimal(str(pipeline or 0)) * current["rate_percent"] / Decimal(100)).quantize(Decimal("0.01"))),
    }

    board = compute_leaderboard(db, organization.id, days)
    leaderboard = [
        {"rank": item["rank"], "name": item["name"], "leads": item["leads"], "inspections": item["inspections_attended"] or item["inspections_booked"], "reservations": item["reservations"], "sales": item["sales"], "is_you": (item["subject_type"], item["subject_id"]) == (member.subject_type, member.subject_id)}
        for item in board[:15]
    ]
    db.commit()
    return {
        "agent": {"name": agent_name, "phone": member.contact_phone, "email": member.contact_email},
        "estates": kit_estates,
        "followups": followups,
        "inspections": inspections,
        "commission": commission,
        "leaderboard": {"period_days": days, "rows": leaderboard},
    }


@router.get("/agent-portal/{token}/marketing/{estate_id}/flyer.pdf")
def agent_flyer(token: str, estate_id: int, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    estate = _agent_estate(db, organization.id, estate_id)
    ctx, _campaign = _agent_ctx(db, member, estate)
    db.commit()
    return _pdf(_render_flyer(ctx), f"{_safe_name(estate.name)}-flyer.pdf")


@router.get("/agent-portal/{token}/marketing/{estate_id}/brochure.pdf")
def agent_brochure(token: str, estate_id: int, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    estate = _agent_estate(db, organization.id, estate_id)
    ctx, _campaign = _agent_ctx(db, member, estate)
    db.commit()
    return _pdf(_render_brochure(ctx), f"{_safe_name(estate.name)}-brochure.pdf")


@router.get("/agent-portal/{token}/marketing/{estate_id}/ad.png")
def agent_estate_ad(token: str, estate_id: int, format: str = "post", db: Session = Depends(get_db)):
    fmt = _ad_format(format)
    _row, member, organization = _agent_portal_context(db, token)
    estate = _agent_estate(db, organization.id, estate_id)
    ctx, _campaign = _agent_ctx(db, member, estate)
    db.commit()
    return _png(_render_estate_ad(ctx, fmt), filename=f"{_safe_name(estate.name)}-{fmt}.png")


@router.get("/agent-portal/{token}/marketing/{estate_id}/plots/{plot_id}/ad.png")
def agent_plot_ad(token: str, estate_id: int, plot_id: int, format: str = "post", db: Session = Depends(get_db)):
    fmt = _ad_format(format)
    _row, member, organization = _agent_portal_context(db, token)
    estate = _agent_estate(db, organization.id, estate_id)
    ctx, _campaign = _agent_ctx(db, member, estate)
    plot = _ctx_plot(ctx, plot_id)
    db.commit()
    return _png(_render_plot_ad(ctx, plot, fmt), filename=f"{_safe_name(estate.name)}-plot-{_safe_name(plot.plot_number)}-{fmt}.png")


@router.patch("/agent-portal/{token}/leads/{lead_id}")
def agent_update_lead(token: str, lead_id: int, payload: AgentLeadUpdate, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    lead = db.get(EstatePublicReservationRequest, lead_id)
    if lead is None or lead.organization_id != organization.id or lead.assigned_agent_subject_type != member.subject_type or lead.assigned_agent_subject_id != member.subject_id:
        raise HTTPException(404, "Lead not found")
    if lead.status in {"converted"}:
        raise HTTPException(409, "This lead has already been converted")
    lead.status = payload.status
    lead.follow_up_reminder_count = 0
    lead.last_follow_up_reminder_at = None
    if payload.status == "contacted":
        lead.contacted_at = _now()
    if payload.note:
        lead.staff_notes = ((lead.staff_notes + "\n") if lead.staff_notes else "") + f"[{member.subject_id}] {payload.note.strip()}"
    append_estate_audit_event(db, organization_id=organization.id, actor=EstatePrincipal(member.subject_type, member.subject_id, member.subject_id), action="public_reservation.agent_updated", entity_type="estate_public_reservation_request", entity_id=lead.id, after_data={"status": lead.status})
    db.commit()
    return _followup_item(db, lead)


@router.patch("/agent-portal/{token}/inspection-bookings/{booking_id}")
def agent_update_booking(token: str, booking_id: int, payload: AgentBookingUpdate, db: Session = Depends(get_db)):
    _row, member, organization = _agent_portal_context(db, token)
    booking = db.get(EstateInspectionBooking, booking_id)
    if booking is None or booking.organization_id != organization.id or booking.assigned_agent_subject_type != member.subject_type or booking.assigned_agent_subject_id != member.subject_id:
        raise HTTPException(404, "Booking not found")
    if booking.status == "cancelled":
        raise HTTPException(409, "This booking was cancelled")
    booking.status = payload.status
    append_estate_audit_event(db, organization_id=organization.id, actor=EstatePrincipal(member.subject_type, member.subject_id, member.subject_id), action="inspection_booking.agent_updated", entity_type="estate_inspection_booking", entity_id=booking.id, after_data={"status": booking.status})
    db.commit()
    return _booking_payload(db, booking)
