from __future__ import annotations

"""Estate social posting: caption templates, scheduled posts, Facebook/Instagram connection, WhatsApp opt-ins."""

import html
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstateOrganization, EstatePlot
from app.models.estate_social import EstateMarketingOptin, EstateSocialAccount, EstateSocialPost, EstateWhatsappSend
from app.routers.estate_marketing import _campaign_for_staff, _png, _staff
from app.routers.plots import get_db
from app.services.estates import marketing_render, social_broadcast, social_meta, social_posts, social_templates, social_whatsapp
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import require_estate_access
from app.services.estates.marketing_common import client_ip, throttled, web_url
from app.services.estates.social_templates import ALL_CHANNELS, AUTOMATIC_CHANNELS, CHANNEL_FORMAT, CHANNEL_LABEL, MANUAL_CHANNELS
from app.utils.secret_box import SecretNotConfigured, decrypt_text, encrypt_text, make_signed_token, read_signed_token, secret_configured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estates", tags=["estate-social"])

WRITE = "marketing.manage"
STYLES = {"promo", "luxury"}
EDITABLE = ("draft", "scheduled", "failed", "partial")


# ── Schemas ──────────────────────────────────────────────────────────────────────────────────
class PostCreate(BaseModel):
    template_key: str = Field(default="custom", max_length=40)
    caption: str = Field(min_length=1, max_length=2200)
    channels: list[str] = Field(min_length=1, max_length=5)
    image_style: str = "promo"
    plot_id: int | None = None
    campaign_id: int | None = None
    scheduled_at: datetime | None = None
    publish_now: bool = False


class PostUpdate(BaseModel):
    caption: str | None = Field(default=None, min_length=1, max_length=2200)
    channels: list[str] | None = Field(default=None, min_length=1, max_length=5)
    image_style: str | None = None
    scheduled_at: datetime | None = None
    clear_schedule: bool = False
    cancel: bool = False


class MarkPosted(BaseModel):
    channel: str


class OptinCreate(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    phone: str = Field(min_length=6, max_length=32)
    consent: bool = False
    source: str | None = Field(default=None, max_length=120)


class BroadcastCreate(BaseModel):
    preset: str
    detail: str | None = Field(default=None, max_length=200)


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────
def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _check_channels(channels: list[str]) -> list[str]:
    clean = []
    for channel in channels:
        if channel not in ALL_CHANNELS:
            raise HTTPException(422, f"Unknown channel: {channel}")
        if channel not in clean:
            clean.append(channel)
    return clean


def _check_schedule(value: datetime | None) -> datetime | None:
    when = _aware(value)
    if when is not None and when < datetime.now(timezone.utc) - timedelta(minutes=1):
        raise HTTPException(422, "Choose a time in the future")
    return when


def _post_payload(post: EstateSocialPost) -> dict:
    return {
        "id": post.id, "estate_id": post.estate_id, "plot_id": post.plot_id, "template_key": post.template_key, "caption": post.caption,
        "channels": list(post.channels or []), "image_style": post.image_style, "status": post.status, "scheduled_at": post.scheduled_at,
        "published_at": post.published_at, "reminder_sent_at": post.reminder_sent_at, "results": post.results or {}, "created_at": post.created_at,
    }


def _post_for_staff(db: Session, request: Request, post_id: int, permission: str = WRITE) -> tuple[EstateSocialPost, Estate, object]:
    post = db.get(EstateSocialPost, post_id)
    if post is None:
        raise HTTPException(404, "Post not found")
    estate = db.get(Estate, post.estate_id)
    if estate is None:
        raise HTTPException(404, "Estate not found")
    access = require_estate_access(db, request, post.organization_id, permission=permission)
    return post, estate, access


def _require_automatic_accounts(db: Session, organization_id: int, channels: list[str]) -> None:
    for channel in channels:
        if channel in AUTOMATIC_CHANNELS:
            if not social_meta.configured():
                raise HTTPException(503, "Facebook and Instagram posting is not switched on for this server yet.")
            if social_posts._account_for(db, organization_id, channel) is None:
                raise HTTPException(409, f"Connect a {CHANNEL_LABEL[channel]} account first (Social posts tab).")


def _company_name(db: Session, estate: Estate) -> str:
    organization = db.get(EstateOrganization, estate.organization_id)
    return str(getattr(organization, "name", None) or "the developer")


# ── Overview, templates, image ───────────────────────────────────────────────────────────────
@router.get("/{estate_id}/marketing/social/overview")
def social_overview(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    accounts = db.query(EstateSocialAccount).filter(EstateSocialAccount.organization_id == estate.organization_id).order_by(EstateSocialAccount.provider, EstateSocialAccount.name).all()
    return {
        "meta_available": social_meta.configured(),
        "whatsapp_available": social_whatsapp.configured(),
        "accounts": [social_posts.account_public(item) for item in accounts],
        "channels": [{"key": key, "label": CHANNEL_LABEL[key], "automatic": key in AUTOMATIC_CHANNELS, "format": CHANNEL_FORMAT[key]} for key in ALL_CHANNELS],
        "published": bool(estate.public_enabled and estate.public_slug),
    }


@router.get("/{estate_id}/marketing/social/templates")
def social_templates_endpoint(estate_id: int, request: Request, plot_id: int | None = None, campaign_id: int | None = None, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first - posts link to it.")
    campaign = _campaign_for_staff(db, estate, campaign_id)
    ctx = marketing_render.build_context(db, estate, source=campaign.code if campaign else None)
    plot = next((item for item in ctx.plots if item.id == plot_id), None) if plot_id else None
    items = social_templates.build_templates(db, ctx, plot)
    db.commit()
    return {"templates": items, "source": ctx.source}


@router.get("/{estate_id}/marketing/social/image.png")
def social_image(estate_id: int, request: Request, channel: str = "facebook", style: str = "promo", plot_id: int | None = None, campaign_id: int | None = None, download: bool = False, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    if channel not in ALL_CHANNELS or style not in STYLES:
        raise HTTPException(422, "Unknown channel or design")
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first.")
    campaign = _campaign_for_staff(db, estate, campaign_id)
    source = campaign.code if campaign else None
    name = estate.name
    db.commit()
    try:
        data = social_posts.render_image(db, estate, fmt=CHANNEL_FORMAT[channel], style=style, plot_id=plot_id, source=source)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _png(data, filename=f"{name.replace(' ', '-')}-{channel}.png" if download else None)


@router.get("/marketing/social/image/{token}.png")
def public_social_image(token: str, db: Session = Depends(get_db)):
    """Fetched by Facebook/Instagram's servers. The link is signed and expires after 24 hours."""
    parsed = social_posts.read_image_token(token)
    if parsed is None:
        raise HTTPException(404, "Image not found")
    post_id, fmt = parsed
    post = db.get(EstateSocialPost, post_id)
    estate = db.get(Estate, post.estate_id) if post else None
    if post is None or estate is None or fmt not in {"post", "status", "landscape", "poster"}:
        raise HTTPException(404, "Image not found")
    style, plot_id, source = post.image_style, post.plot_id, post.source_code
    db.commit()
    return _png(social_posts.render_image(db, estate, fmt=fmt, style=style, plot_id=plot_id, source=source), public=True)


# ── Posts ────────────────────────────────────────────────────────────────────────────────────
@router.get("/{estate_id}/marketing/social/posts")
def list_posts(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    rows = db.query(EstateSocialPost).filter(EstateSocialPost.estate_id == estate.id).order_by(EstateSocialPost.created_at.desc()).limit(50).all()
    return {"items": [_post_payload(row) for row in rows]}


@router.post("/{estate_id}/marketing/social/posts", status_code=201)
def create_post(estate_id: int, payload: PostCreate, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first - posts link to it.")
    if payload.image_style not in STYLES:
        raise HTTPException(422, "Choose the promo or classic design")
    channels = _check_channels(payload.channels)
    when = _check_schedule(payload.scheduled_at)
    if payload.publish_now and when is not None:
        raise HTTPException(422, "Choose either post now or a schedule")
    plot = None
    if payload.plot_id:
        plot = db.get(EstatePlot, payload.plot_id)
        if plot is None or plot.estate_id != estate.id:
            raise HTTPException(404, "Plot not found")
    campaign = _campaign_for_staff(db, estate, payload.campaign_id)
    if payload.publish_now or when is not None:
        _require_automatic_accounts(db, estate.organization_id, channels)
    post = EstateSocialPost(
        organization_id=estate.organization_id, estate_id=estate.id, plot_id=plot.id if plot else None, template_key=payload.template_key[:40],
        caption=payload.caption.strip(), image_format=CHANNEL_FORMAT[channels[0]], image_style=payload.image_style, source_code=campaign.code if campaign else None,
        channels=channels, status="scheduled" if when is not None else "draft", scheduled_at=when, results={},
        created_by_subject_type=access.principal.subject_type, created_by_subject_id=str(access.principal.subject_id),
    )
    db.add(post)
    db.flush()
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="social_post.created", entity_type="estate_social_post", entity_id=post.id, after_data={"channels": channels, "scheduled_at": when.isoformat() if when else None})
    db.commit()
    if payload.publish_now:
        social_posts.publish_post(db, post)
    return _post_payload(post)


@router.patch("/marketing/social/posts/{post_id}")
def update_post(post_id: int, payload: PostUpdate, request: Request, db: Session = Depends(get_db)):
    post, estate, access = _post_for_staff(db, request, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(409, "This post has already been sent or is being sent")
    if payload.cancel:
        post.status = "cancelled"
        post.scheduled_at = None
    else:
        if payload.caption is not None:
            post.caption = payload.caption.strip()
        if payload.image_style is not None:
            if payload.image_style not in STYLES:
                raise HTTPException(422, "Choose the promo or classic design")
            post.image_style = payload.image_style
        if payload.channels is not None:
            post.channels = _check_channels(payload.channels)
        if payload.clear_schedule:
            post.scheduled_at = None
            post.status = "draft"
        elif payload.scheduled_at is not None:
            _require_automatic_accounts(db, post.organization_id, list(post.channels or []))
            post.scheduled_at = _check_schedule(payload.scheduled_at)
            post.status = "scheduled"
            post.reminder_sent_at = None
    append_estate_audit_event(db, organization_id=post.organization_id, actor=access.principal, action="social_post.updated", entity_type="estate_social_post", entity_id=post.id, after_data={"status": post.status})
    db.commit()
    return _post_payload(post)


@router.post("/marketing/social/posts/{post_id}/publish")
def publish_now(post_id: int, request: Request, db: Session = Depends(get_db)):
    post, estate, access = _post_for_staff(db, request, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(409, "This post has already been sent or is being sent")
    _require_automatic_accounts(db, post.organization_id, list(post.channels or []))
    append_estate_audit_event(db, organization_id=post.organization_id, actor=access.principal, action="social_post.publish_requested", entity_type="estate_social_post", entity_id=post.id)
    db.commit()
    social_posts.publish_post(db, post)
    return _post_payload(post)


@router.post("/marketing/social/posts/{post_id}/mark-posted")
def mark_posted(post_id: int, payload: MarkPosted, request: Request, db: Session = Depends(get_db)):
    post, _estate, _access = _post_for_staff(db, request, post_id)
    try:
        social_posts.mark_manual_posted(db, post, payload.channel)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _post_payload(post)


# ── Facebook / Instagram connection ──────────────────────────────────────────────────────────
@router.post("/{estate_id}/marketing/social/meta/connect")
def meta_connect(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    if not social_meta.configured():
        raise HTTPException(503, "Facebook and Instagram posting is not switched on for this server yet.")
    state = make_signed_token("metastate", estate.organization_id, estate.id, access.principal.subject_type, access.principal.subject_id, ttl_seconds=900)
    return {"url": social_meta.oauth_url(state)}


def _back(estate_id: int, **params: str) -> RedirectResponse:
    query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
    return RedirectResponse(f"{web_url()}/estates/{estate_id}/marketing?tab=social&{query}", status_code=302)


@router.get("/marketing/social/meta/callback")
def meta_callback(code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None, db: Session = Depends(get_db)):
    parts = read_signed_token(state or "", "metastate")
    if not parts or len(parts) != 4:
        return HTMLResponse("<p>This connection link has expired. Close this window and try again from LandCheck.</p>", status_code=400)
    organization_id, estate_id, subject_type, subject_id = int(parts[0]), int(parts[1]), parts[2], parts[3]
    if error or not code:
        return _back(estate_id, connect_error=error_description or "Facebook did not grant access")
    try:
        granted = social_meta.exchange_code(code)
    except social_meta.MetaError as exc:
        return _back(estate_id, connect_error=str(exc))
    except Exception:
        logger.exception("Meta connect failed")
        return _back(estate_id, connect_error="Could not reach Facebook. Please try again.")
    stored = 0
    for page in granted["pages"]:
        token_enc = encrypt_text(page["access_token"])
        candidates = [("facebook", str(page["id"]), page.get("name") or "Facebook Page", None, None)]
        instagram = page.get("instagram_business_account")
        if instagram and instagram.get("id"):
            candidates.append(("instagram", str(instagram["id"]), instagram.get("name") or instagram.get("username") or "Instagram", instagram.get("username"), str(page["id"])))
        for provider, external_id, name, username, linked in candidates:
            row = db.query(EstateSocialAccount).filter(EstateSocialAccount.organization_id == organization_id, EstateSocialAccount.provider == provider, EstateSocialAccount.external_id == external_id).one_or_none()
            if row is None:
                row = EstateSocialAccount(organization_id=organization_id, provider=provider, external_id=external_id, name=name, username=username, linked_page_id=linked, access_token_enc=token_enc)
                db.add(row)
            row.name, row.username, row.linked_page_id = name, username, linked
            row.access_token_enc = token_enc
            row.status = "active"
            row.facebook_user_id = granted.get("facebook_user_id")
            row.connected_by_subject_type, row.connected_by_subject_id = subject_type, subject_id
            stored += 1
    db.commit()
    if not stored:
        return _back(estate_id, connect_error="No Facebook Pages were shared. Choose at least one Page when asked.")
    return _back(estate_id, connected=str(stored))


@router.delete("/marketing/social/accounts/{account_id}", status_code=204)
def disconnect_account(account_id: int, request: Request, db: Session = Depends(get_db)):
    account = db.get(EstateSocialAccount, account_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    access = require_estate_access(db, request, account.organization_id, permission=WRITE)
    append_estate_audit_event(db, organization_id=account.organization_id, actor=access.principal, action="social_account.disconnected", entity_type="estate_social_account", entity_id=account.id, after_data={"provider": account.provider, "name": account.name})
    db.delete(account)
    db.commit()
    return Response(status_code=204)


def _forget_facebook_user(db: Session, facebook_user_id: str) -> int:
    rows = db.query(EstateSocialAccount).filter(EstateSocialAccount.facebook_user_id == facebook_user_id).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


@router.post("/marketing/social/meta/deauthorize")
def meta_deauthorize(signed_request: str = Form(...), db: Session = Depends(get_db)):
    data = social_meta.parse_signed_request(signed_request)
    if not data or not data.get("user_id"):
        raise HTTPException(400, "Invalid request")
    _forget_facebook_user(db, str(data["user_id"]))
    return {"ok": True}


@router.post("/marketing/social/meta/data-deletion")
def meta_data_deletion(signed_request: str = Form(...), db: Session = Depends(get_db)):
    """Meta calls this when someone removes the app and asks for their data to be deleted."""
    data = social_meta.parse_signed_request(signed_request)
    if not data or not data.get("user_id"):
        raise HTTPException(400, "Invalid request")
    _forget_facebook_user(db, str(data["user_id"]))
    code = uuid.uuid4().hex[:16]
    return {"url": f"{web_url()}/data-deletion?code={code}", "confirmation_code": code}


# ── WhatsApp opt-ins and broadcasts ──────────────────────────────────────────────────────────
@router.get("/marketing/public/{slug}/optin-info")
def optin_info(slug: str, db: Session = Depends(get_db)):
    estate = db.query(Estate).filter(Estate.public_slug == slug, Estate.public_enabled.is_(True), Estate.archived_at.is_(None)).one_or_none()
    if estate is None:
        raise HTTPException(404, "Estate not found")
    return {"enabled": social_whatsapp.configured(), "consent_text": social_broadcast.consent_text(estate.name, _company_name(db, estate))}


@router.post("/marketing/public/{slug}/optin", status_code=201)
def create_optin(slug: str, payload: OptinCreate, request: Request, db: Session = Depends(get_db)):
    if throttled(client_ip(request), "optin", limit=8, window_seconds=600):
        raise HTTPException(429, "Too many requests. Please try again shortly.")
    estate = db.query(Estate).filter(Estate.public_slug == slug, Estate.public_enabled.is_(True), Estate.archived_at.is_(None)).one_or_none()
    if estate is None:
        raise HTTPException(404, "Estate not found")
    if not social_whatsapp.configured():
        raise HTTPException(503, "WhatsApp updates are not available yet")
    if not payload.consent:
        raise HTTPException(422, "Please tick the box to agree to receive WhatsApp updates")
    try:
        social_broadcast.record_optin(db, estate=estate, company_name=_company_name(db, estate), full_name=payload.full_name, phone=payload.phone, source_code=payload.source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {"ok": True, "message": "You are subscribed. Reply STOP to any message to unsubscribe."}


@router.get("/{estate_id}/marketing/social/optins")
def list_optins(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    active = db.query(func.count(EstateMarketingOptin.id)).filter(EstateMarketingOptin.estate_id == estate.id, EstateMarketingOptin.status == "active").scalar() or 0
    revoked = db.query(func.count(EstateMarketingOptin.id)).filter(EstateMarketingOptin.estate_id == estate.id, EstateMarketingOptin.status == "revoked").scalar() or 0
    rows = db.query(EstateMarketingOptin).filter(EstateMarketingOptin.estate_id == estate.id).order_by(EstateMarketingOptin.consented_at.desc()).limit(50).all()
    batches = db.query(EstateWhatsappSend.batch_uid, EstateWhatsappSend.status, func.count(EstateWhatsappSend.id), func.min(EstateWhatsappSend.created_at)).filter(EstateWhatsappSend.estate_id == estate.id).group_by(EstateWhatsappSend.batch_uid, EstateWhatsappSend.status).all()
    summary: dict[str, dict] = {}
    for batch, status, count, created in batches:
        entry = summary.setdefault(batch, {"batch": batch, "created_at": created, "sent": 0, "failed": 0, "queued": 0, "skipped": 0})
        entry[status] = entry.get(status, 0) + int(count)
        entry["created_at"] = min(entry["created_at"], created) if created else entry["created_at"]
    recent = sorted(summary.values(), key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:5]
    return {
        "whatsapp_available": social_whatsapp.configured(), "active": int(active), "revoked": int(revoked),
        "presets": social_whatsapp.presets_payload(), "batches": recent,
        "items": [{"id": row.id, "name": row.full_name, "phone": f"+{row.phone_digits}", "status": row.status, "consented_at": row.consented_at, "source": row.source_code} for row in rows],
    }


@router.post("/{estate_id}/marketing/social/whatsapp/broadcast", status_code=202)
def whatsapp_broadcast(estate_id: int, payload: BroadcastCreate, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    if not social_whatsapp.configured():
        raise HTTPException(503, "WhatsApp messaging is not switched on for this server yet.")
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first - messages link to it.")
    ctx = marketing_render.build_context(db, estate, source=None)
    try:
        batch, count = social_broadcast.queue_broadcast(db, estate=estate, preset=payload.preset, detail=payload.detail, min_price=ctx.min_price if ctx.show_prices else None, sent_by=str(access.principal.subject_id))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not count:
        raise HTTPException(409, "Nobody has opted in to WhatsApp updates for this estate yet.")
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="whatsapp_broadcast.queued", entity_type="estate", entity_id=estate.id, after_data={"preset": payload.preset, "recipients": count, "batch": batch})
    db.commit()
    return {"batch": batch, "queued": count}


@router.delete("/marketing/social/optins/{optin_id}", status_code=204)
def revoke_optin(optin_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateMarketingOptin, optin_id)
    if row is None:
        raise HTTPException(404, "Contact not found")
    require_estate_access(db, request, row.organization_id, permission=WRITE)
    row.status = "revoked"
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


@router.get("/marketing/social/whatsapp/webhook")
def whatsapp_webhook_verify(request: Request):
    """Meta calls this once to check the webhook address."""
    params = request.query_params
    expected = str(os.getenv("WHATSAPP_VERIFY_TOKEN") or "").strip()
    if expected and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
        return PlainTextResponse(params.get("hub.challenge") or "")
    raise HTTPException(403, "Verification failed")


@router.post("/marketing/social/whatsapp/webhook")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    """Handles STOP replies so opted-out people are never messaged again."""
    raw = await request.body()
    if not social_whatsapp.verify_webhook_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "Invalid signature")
    try:
        body = await request.json()
    except ValueError:
        return {"ok": True}
    stopped = 0
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            for message in ((change.get("value") or {}).get("messages") or []):
                text = ((message.get("text") or {}).get("body")) if message.get("type") == "text" else None
                if social_whatsapp.is_stop_message(text) and message.get("from"):
                    stopped += social_broadcast.revoke_phone_everywhere(db, str(message["from"]))
    db.commit()
    return {"ok": True, "stopped": stopped}
