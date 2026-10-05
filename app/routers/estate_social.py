from __future__ import annotations

"""Estate social posting: caption templates, scheduled posts, Facebook/Instagram connection, WhatsApp opt-ins."""

import html
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstateAllocation, EstateCustomer, EstateOrganization, EstatePlot
from app.models.estate_social import EstateMarketingOptin, EstateSocialAccount, EstateSocialPlan, EstateSocialPost, EstateWhatsappMessage, EstateWhatsappSend
from app.routers.estate_marketing import NO_CACHE_PRIVATE, _campaign_for_staff, _png, _staff
from app.routers.plots import get_db
from app.services.estates import marketing_render, social_broadcast, social_meta, social_planner, social_posts, social_templates, social_whatsapp, whatsapp_inbox
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import require_estate_access
from app.services.estates.marketing_common import client_ip, normalize_phone_digits, public_page_url, throttled, web_url
from app.services.estates.subscriptions import get_subscription, has_auto_posting_access
from app.services.estates.social_templates import ALL_CHANNELS, AUTOMATIC_CHANNELS, CHANNEL_FORMAT, CHANNEL_LABEL, MANUAL_CHANNELS
from app.utils.secret_box import SecretNotConfigured, decrypt_text, encrypt_text, make_signed_token, read_signed_token, secret_configured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estates", tags=["estate-social"])

WRITE = "marketing.manage"
STYLES = {"promo", "luxury", "heritage", "bold", "blueprint"}
EDITABLE = ("draft", "scheduled", "failed", "partial")


# ── Schemas ──────────────────────────────────────────────────────────────────────────────────
class PostCreate(BaseModel):
    template_key: str = Field(default="custom", max_length=40)
    caption: str = Field(min_length=1, max_length=2200)
    channels: list[str] = Field(min_length=1, max_length=5)
    image_style: str = "promo"
    include_media: bool = True
    plot_id: int | None = None
    campaign_id: int | None = None
    scheduled_at: datetime | None = None
    publish_now: bool = False


class PostUpdate(BaseModel):
    caption: str | None = Field(default=None, min_length=1, max_length=2200)
    channels: list[str] | None = Field(default=None, min_length=1, max_length=5)
    image_style: str | None = None
    include_media: bool | None = None
    scheduled_at: datetime | None = None
    clear_schedule: bool = False
    cancel: bool = False


class PlanCreate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    days: int = Field(default=7, ge=1, le=30)
    per_day: int | None = Field(default=None, ge=1, le=3)
    per_week: int | None = Field(default=None, ge=1, le=7)
    times: list[str] = Field(default_factory=list, max_length=3)
    channels: list[str] = Field(min_length=1, max_length=5)
    tone: str = "friendly"
    style: str = "mixed"
    start_date: date | None = None
    campaign_id: int | None = None
    auto_renew: bool = False
    approve: bool = True


class PlanUpdate(BaseModel):
    action: str | None = None  # pause | resume | stop | approve
    auto_renew: bool | None = None


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
    image_style: str | None = None


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


def _check_media_channels(include_media: bool, channels: list[str]) -> None:
    """Instagram (feed post or story) has no text-only post type - every post needs an image."""
    if include_media:
        return
    labels = [CHANNEL_LABEL[channel] for channel in channels if channel in {"instagram", "instagram_story"}]
    if labels:
        raise HTTPException(422, f"{' and '.join(labels)} require{'s' if len(labels) == 1 else ''} an image - turn media back on, or remove {'it' if len(labels) == 1 else 'them'} from this post's channels.")


def _check_schedule(value: datetime | None) -> datetime | None:
    when = _aware(value)
    if when is not None and when < datetime.now(timezone.utc) - timedelta(minutes=1):
        raise HTTPException(422, "Choose a time in the future")
    return when


def _post_payload(post: EstateSocialPost) -> dict:
    return {
        "id": post.id, "estate_id": post.estate_id, "plot_id": post.plot_id, "template_key": post.template_key, "caption": post.caption,
        "channels": list(post.channels or []), "image_style": post.image_style, "include_media": post.include_media, "status": post.status, "scheduled_at": post.scheduled_at,
        "published_at": post.published_at, "reminder_sent_at": post.reminder_sent_at, "results": post.results or {}, "created_at": post.created_at,
        "plan_id": post.plan_id, "auto_caption": bool(post.auto_caption),
        "template_label": social_templates.TEMPLATE_INFO.get(post.template_key, (None,))[0], "strategy": (social_templates.TEMPLATE_INFO.get(post.template_key) or (None, None))[1],
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
            _require_auto_posting(db, organization_id)
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
        "auto_posting": has_auto_posting_access(get_subscription(db, estate.organization_id)),
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
def social_image(estate_id: int, request: Request, channel: str = "facebook", style: str = "promo", plot_id: int | None = None, campaign_id: int | None = None, source: str | None = None, download: bool = False, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    if channel not in ALL_CHANNELS or style not in STYLES:
        raise HTTPException(422, "Unknown channel or design")
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first.")
    campaign = _campaign_for_staff(db, estate, campaign_id)
    source = campaign.code if campaign else (source[:120] if source else None)
    name = estate.name
    db.commit()
    try:
        data = social_posts.render_image(db, estate, fmt=CHANNEL_FORMAT[channel], style=style, plot_id=plot_id, source=source)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        logger.exception(
            "Social post image preview failed (estate_id=%s, channel=%s, style=%s, plot_id=%s)",
            estate_id, channel, style, plot_id,
        )
        raise HTTPException(502, "This image could not be generated - check the estate's cover photo and plot data in Settings, then try again.") from None
    return _png(data, filename=f"{name.replace(' ', '-')}-{channel}.png" if download else None)


@router.get("/marketing/social/whatsapp-image/{token}.png")
def public_whatsapp_image(token: str, db: Session = Depends(get_db)):
    """Fetched by WhatsApp's servers as a template header image. The link is signed and expires after 24 hours."""
    parsed = social_posts.read_broadcast_image_token(token)
    if parsed is None:
        raise HTTPException(404, "Image not found")
    estate_id, style = parsed
    estate = db.get(Estate, estate_id)
    if estate is None or style not in marketing_render.AD_STYLES:
        raise HTTPException(404, "Image not found")
    data = social_posts.render_image(db, estate, fmt="landscape", style=style, plot_id=None, source="whatsapp")
    db.commit()
    return _png(data, public=True)


@router.get("/marketing/social/image/{token}.png")
def public_social_image(token: str, db: Session = Depends(get_db)):
    """Fetched by Facebook/Instagram's servers. The link is signed and expires after 24 hours.

    If rendering fails, Facebook/Instagram's own error for this ("Make sure your Page post includes
    an image that can be used in an ad") is a generic catch-all for "we couldn't fetch/parse a usable
    image from that URL" - it gives no detail at all about what actually went wrong on our side. So this
    logs the real exception with enough context to diagnose it, instead of letting FastAPI's default
    500 handler swallow it into an opaque response."""
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
    try:
        data = social_posts.render_image(db, estate, fmt=fmt, style=style, plot_id=plot_id, source=source)
    except Exception:
        logger.exception(
            "Social post image render failed (post_id=%s, estate_id=%s, fmt=%s, style=%s, plot_id=%s)",
            post_id, estate.id, fmt, style, plot_id,
        )
        raise HTTPException(502, "The post image could not be generated. Check this estate's cover photo and plot data, then try again.") from None
    return _png(data, public=True)


# ── Posts ────────────────────────────────────────────────────────────────────────────────────
SCOPE_STATUSES = {
    "upcoming": ("scheduled", "publishing"),
    "posted": ("published", "partial"),
    "attention": ("failed", "partial", "skipped"),
    "drafts": ("draft",),
    "cancelled": ("cancelled",),
}


@router.get("/{estate_id}/marketing/social/posts")
def list_posts(estate_id: int, request: Request, scope: str = "all", plan_id: int | None = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """Every post for the estate. `scope` narrows it (upcoming, posted, attention, drafts, cancelled); the
    counts always describe the whole estate so tabs can show their totals."""
    estate, _access = _staff(db, request, estate_id)
    base = db.query(EstateSocialPost).filter(EstateSocialPost.estate_id == estate.id)
    if plan_id:
        base = base.filter(EstateSocialPost.plan_id == plan_id)
    by_status = dict(base.with_entities(EstateSocialPost.status, func.count(EstateSocialPost.id)).group_by(EstateSocialPost.status).all())
    counts = {name: sum(int(by_status.get(status, 0)) for status in statuses) for name, statuses in SCOPE_STATUSES.items()}
    counts["all"] = sum(int(value) for value in by_status.values())
    query = base
    if scope in SCOPE_STATUSES:
        query = query.filter(EstateSocialPost.status.in_(SCOPE_STATUSES[scope]))
    moment = func.coalesce(EstateSocialPost.scheduled_at, EstateSocialPost.created_at)
    query = query.order_by(moment.asc() if scope == "upcoming" else moment.desc(), EstateSocialPost.id.asc())
    rows = query.offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    plan_ids = {row.plan_id for row in rows if row.plan_id}
    plan_names = {plan.id: plan.name for plan in db.query(EstateSocialPlan).filter(EstateSocialPlan.id.in_(plan_ids or {-1})).all()}
    items = [{**_post_payload(row), "source_code": row.source_code, "plan_name": plan_names.get(row.plan_id)} for row in rows]
    plans = db.query(EstateSocialPlan).filter(EstateSocialPlan.estate_id == estate.id).order_by(EstateSocialPlan.created_at.desc()).limit(30).all()
    return {"items": items, "counts": counts, "plans": [{"id": plan.id, "name": plan.name, "status": plan.status} for plan in plans]}


@router.post("/{estate_id}/marketing/social/posts", status_code=201)
def create_post(estate_id: int, payload: PostCreate, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first - posts link to it.")
    if payload.image_style not in STYLES:
        raise HTTPException(422, "Choose the promo or classic design")
    channels = _check_channels(payload.channels)
    _check_media_channels(payload.include_media, channels)
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
        caption=payload.caption.strip(), image_format=CHANNEL_FORMAT[channels[0]], image_style=payload.image_style, include_media=payload.include_media, source_code=campaign.code if campaign else None,
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
    revive = post.status in ("skipped", "cancelled") and payload.scheduled_at is not None and not payload.cancel
    if post.status not in EDITABLE and not revive:
        raise HTTPException(409, "This post has already been sent or is being sent")
    if revive:  # a skipped or cancelled post given a new time is a fresh scheduled post
        post.results = {}
        post.reminder_sent_at = None
    if payload.cancel:
        post.status = "cancelled"
        post.scheduled_at = None
    else:
        if payload.caption is not None:
            post.caption = payload.caption.strip()
            post.auto_caption = False  # the company's own words are kept exactly as written
        if payload.image_style is not None:
            if payload.image_style not in STYLES:
                raise HTTPException(422, "Choose the promo or classic design")
            post.image_style = payload.image_style
        if payload.channels is not None:
            post.channels = _check_channels(payload.channels)
        if payload.include_media is not None:
            post.include_media = payload.include_media
        _check_media_channels(post.include_media, list(post.channels or []))
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


@router.post("/marketing/social/posts/{post_id}/rewrite")
def rewrite_post(post_id: int, request: Request, db: Session = Depends(get_db)):
    """Swap in another wording of the same post, written from today's plot data."""
    post, _estate, _access = _post_for_staff(db, request, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(409, "This post has already been sent or is being sent")
    if not social_planner.rewrite_post(db, post):
        raise HTTPException(409, "There is no other wording for this post. Edit the caption yourself instead.")
    db.commit()
    return _post_payload(post)


@router.post("/marketing/social/posts/{post_id}/channels/{channel}/delete")
def delete_post_channel(post_id: int, channel: str, request: Request, db: Session = Depends(get_db)):
    """Removes the live post from Facebook/Instagram. The LandCheck record stays - its status just
    changes to 'deleted' - so there's still a history of what was posted and when."""
    post, _estate, access = _post_for_staff(db, request, post_id)
    if channel not in list(post.channels or []):
        raise HTTPException(404, "This post was not sent to that channel")
    if ((post.results or {}).get(channel) or {}).get("status") != "ok":
        raise HTTPException(409, "There is nothing live on this channel to delete")
    outcome = social_posts.delete_published(db, post, channel)
    if outcome.get("status") == "failed":
        raise HTTPException(502, outcome.get("error") or "The post could not be deleted.")
    append_estate_audit_event(db, organization_id=post.organization_id, actor=access.principal, action="social_post.deleted", entity_type="estate_social_post", entity_id=post.id, after_data={"channel": channel})
    db.commit()
    return _post_payload(post)


@router.post("/marketing/social/posts/{post_id}/channels/{channel}/refresh-stats")
def refresh_post_channel_stats(post_id: int, channel: str, request: Request, db: Session = Depends(get_db)):
    post, _estate, _access = _post_for_staff(db, request, post_id)
    if channel not in list(post.channels or []):
        raise HTTPException(404, "This post was not sent to that channel")
    outcome = social_posts.refresh_stats(db, post, channel)
    if outcome.get("status") == "failed":
        raise HTTPException(502, outcome.get("error") or "Engagement stats could not be loaded.")
    return _post_payload(post)


# ── Content plans (auto-written, auto-scheduled posts) ───────────────────────────────────────
def _plan_payload(db: Session, plan: EstateSocialPlan) -> dict:
    counts = dict(db.query(EstateSocialPost.status, func.count(EstateSocialPost.id)).filter(EstateSocialPost.plan_id == plan.id).group_by(EstateSocialPost.status).all())
    nxt = db.query(func.min(EstateSocialPost.scheduled_at)).filter(EstateSocialPost.plan_id == plan.id, EstateSocialPost.status == "scheduled", EstateSocialPost.scheduled_at > datetime.now(timezone.utc)).scalar()
    return {
        "id": plan.id, "name": plan.name, "status": plan.status, "per_day": plan.per_day, "per_week": plan.per_week, "times": list(plan.times or []),
        "channels": list(plan.channels or []), "style": plan.style, "tone": plan.tone, "duration_days": plan.duration_days, "auto_renew": plan.auto_renew,
        "counts": {key: int(value) for key, value in counts.items()}, "next_post_at": nxt, "created_at": plan.created_at,
    }


def _plan_context(db: Session, estate: Estate, campaign_id: int | None):
    if not (estate.public_enabled and estate.public_slug):
        raise HTTPException(409, "Publish this estate's public page first - posts link to it.")
    campaign = _campaign_for_staff(db, estate, campaign_id)
    return marketing_render.build_context(db, estate, source=campaign.code if campaign else None), (campaign.code if campaign else None)


def _preview_item(item: dict) -> dict:
    return {"scheduled_at": item["scheduled_at"], "template_key": item["template_key"], "label": item["label"], "strategy": item["strategy"], "caption": item["caption"], "image_style": item["image_style"], "plot_id": item["plot_id"]}


def _require_auto_posting(db: Session, organization_id: int) -> None:
    if not has_auto_posting_access(get_subscription(db, organization_id)):
        raise HTTPException(
            status_code=402,
            detail={"code": "upgrade_required", "feature": "auto_posting", "message": "Automatic posting plans are available on the Pro and Enterprise plans. Upgrade to unlock them.", "suggested_plan": "pro"},
        )


@router.post("/{estate_id}/marketing/social/plans/preview")
def preview_plan(estate_id: int, payload: PlanCreate, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id, permission=WRITE)
    _require_auto_posting(db, estate.organization_id)
    ctx, _source = _plan_context(db, estate, payload.campaign_id)
    _check_channels(payload.channels)
    try:
        items = social_planner.build_preview(db, ctx, days=payload.days, per_day=payload.per_day, per_week=payload.per_week, times=payload.times, tone=payload.tone, style=payload.style, start=payload.start_date)
    except social_planner.PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {"items": [_preview_item(item) for item in items], "count": len(items)}


@router.post("/{estate_id}/marketing/social/plans", status_code=201)
def create_plan(estate_id: int, payload: PlanCreate, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    _require_auto_posting(db, estate.organization_id)
    ctx, source = _plan_context(db, estate, payload.campaign_id)
    channels = _check_channels(payload.channels)
    _require_automatic_accounts(db, estate.organization_id, channels)
    try:
        plan, posts = social_planner.create_plan(
            db, estate, ctx, name=payload.name, days=payload.days, per_day=payload.per_day, per_week=payload.per_week, times=payload.times, channels=channels, tone=payload.tone,
            style=payload.style, start=payload.start_date, auto_renew=payload.auto_renew, approve=payload.approve, source_code=source,
            subject_type=access.principal.subject_type, subject_id=str(access.principal.subject_id),
        )
    except social_planner.PlanError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    append_estate_audit_event(db, organization_id=estate.organization_id, actor=access.principal, action="social_plan.created", entity_type="estate_social_plan", entity_id=plan.id, after_data={"posts": len(posts), "channels": channels, "auto_renew": plan.auto_renew, "approved": payload.approve})
    db.commit()
    return {**_plan_payload(db, plan), "created_posts": len(posts)}


@router.get("/{estate_id}/marketing/social/plans")
def list_plans(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    plans = db.query(EstateSocialPlan).filter(EstateSocialPlan.estate_id == estate.id, EstateSocialPlan.status.in_(("active", "paused"))).order_by(EstateSocialPlan.created_at.desc()).limit(20).all()
    return {"items": [_plan_payload(db, plan) for plan in plans]}


@router.patch("/marketing/social/plans/{plan_id}")
def update_plan(plan_id: int, payload: PlanUpdate, request: Request, db: Session = Depends(get_db)):
    plan = db.get(EstateSocialPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    access = require_estate_access(db, request, plan.organization_id, permission=WRITE)
    if payload.auto_renew is not None:
        plan.auto_renew = payload.auto_renew
    action = payload.action
    if action == "pause":
        plan.status = "paused"
    elif action == "resume":
        _require_automatic_accounts(db, plan.organization_id, list(plan.channels or []))
        plan.status = "active"
        social_planner.reslot_overdue(db, plan)
    elif action == "stop":
        plan.status = "ended"
        db.query(EstateSocialPost).filter(EstateSocialPost.plan_id == plan.id, EstateSocialPost.status.in_(("scheduled", "draft"))).update({"status": "cancelled", "scheduled_at": None}, synchronize_session=False)
    elif action == "approve":
        _require_automatic_accounts(db, plan.organization_id, list(plan.channels or []))
        social_planner.approve_plan(db, plan)
    elif action is not None:
        raise HTTPException(422, "Unknown action")
    append_estate_audit_event(db, organization_id=plan.organization_id, actor=access.principal, action=f"social_plan.{action or 'updated'}", entity_type="estate_social_plan", entity_id=plan.id, after_data={"status": plan.status, "auto_renew": plan.auto_renew})
    db.commit()
    return _plan_payload(db, plan)


# ── Facebook / Instagram connection ──────────────────────────────────────────────────────────
@router.post("/{estate_id}/marketing/social/meta/connect")
def meta_connect(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    _require_auto_posting(db, estate.organization_id)
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
                # The first connected account for this provider becomes the one auto-posting uses by
                # default - a company connecting several Pages later has to explicitly pick one instead.
                has_default = db.query(EstateSocialAccount).filter(EstateSocialAccount.organization_id == organization_id, EstateSocialAccount.provider == provider, EstateSocialAccount.is_default.is_(True)).first() is not None
                row = EstateSocialAccount(organization_id=organization_id, provider=provider, external_id=external_id, name=name, username=username, linked_page_id=linked, access_token_enc=token_enc, is_default=not has_default)
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


@router.post("/marketing/social/accounts/{account_id}/set-default")
def set_default_account(account_id: int, request: Request, db: Session = Depends(get_db)):
    """Which connected Page (or Instagram account) auto-posting uses, when a company has more than
    one of the same type connected. Only one account per provider can be default at a time."""
    account = db.get(EstateSocialAccount, account_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    access = require_estate_access(db, request, account.organization_id, permission=WRITE)
    db.query(EstateSocialAccount).filter(
        EstateSocialAccount.organization_id == account.organization_id,
        EstateSocialAccount.provider == account.provider,
        EstateSocialAccount.id != account.id,
    ).update({"is_default": False})
    account.is_default = True
    append_estate_audit_event(db, organization_id=account.organization_id, actor=access.principal, action="social_account.set_default", entity_type="estate_social_account", entity_id=account.id, after_data={"provider": account.provider, "name": account.name})
    db.commit()
    return {"ok": True}


@router.delete("/marketing/social/accounts/{account_id}", status_code=204)
def disconnect_account(account_id: int, request: Request, db: Session = Depends(get_db)):
    account = db.get(EstateSocialAccount, account_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    access = require_estate_access(db, request, account.organization_id, permission=WRITE)
    append_estate_audit_event(db, organization_id=account.organization_id, actor=access.principal, action="social_account.disconnected", entity_type="estate_social_account", entity_id=account.id, after_data={"provider": account.provider, "name": account.name})
    was_default = account.is_default
    db.delete(account)
    if was_default:
        # Auto-posting should not go silently dead because the designated Page was disconnected -
        # promote whichever other account of the same type is left, if any.
        next_account = db.query(EstateSocialAccount).filter(EstateSocialAccount.organization_id == account.organization_id, EstateSocialAccount.provider == account.provider, EstateSocialAccount.id != account.id, EstateSocialAccount.status == "active").order_by(EstateSocialAccount.id.asc()).first()
        if next_account is not None:
            next_account.is_default = True
    db.commit()
    return Response(status_code=204)


def _forget_facebook_user(db: Session, facebook_user_id: str) -> int:
    rows = db.query(EstateSocialAccount).filter(EstateSocialAccount.facebook_user_id == facebook_user_id).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


@router.get("/marketing/social/meta/deauthorize", include_in_schema=False)
@router.get("/marketing/social/meta/data-deletion", include_in_schema=False)
def meta_callbacks_for_people():
    """These addresses receive POST requests from Meta. A person opening one in a browser gets the
    plain-language deletion instructions instead of an error."""
    return RedirectResponse(f"{web_url()}/data-deletion", status_code=302)


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
        "whatsapp_available": social_whatsapp.configured(), "template_images": social_whatsapp.template_images_enabled(), "active": int(active), "revoked": int(revoked),
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
    image_style = str(payload.image_style or "").strip().lower()
    if image_style and image_style not in marketing_render.AD_STYLES:
        raise HTTPException(422, "Choose a valid design style")
    try:
        batch, count = social_broadcast.queue_broadcast(db, estate=estate, preset=payload.preset, detail=payload.detail, min_price=ctx.min_price if ctx.show_prices else None, sent_by=str(access.principal.subject_id), image_style=image_style or None)
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
    """Handles STOP replies, and stores every other inbound message in the inbox of whichever estate
    the sender's number resolves to (see whatsapp_inbox.resolve_estate_for_phone)."""
    raw = await request.body()
    if not social_whatsapp.verify_webhook_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "Invalid signature")
    try:
        body = await request.json()
    except ValueError:
        return {"ok": True}
    stopped = 0
    stored = 0
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            for message in ((change.get("value") or {}).get("messages") or []):
                text = ((message.get("text") or {}).get("body")) if message.get("type") == "text" else None
                if social_whatsapp.is_stop_message(text) and message.get("from"):
                    stopped += social_broadcast.revoke_phone_everywhere(db, str(message["from"]))
                    continue
                if whatsapp_inbox.record_inbound(db, message):
                    stored += 1
    db.commit()
    return {"ok": True, "stopped": stopped, "stored": stored}


# ── WhatsApp inbox ───────────────────────────────────────────────────────────────────────────
def _message_payload(row: EstateWhatsappMessage) -> dict:
    return {
        "id": row.id, "direction": row.direction, "type": row.message_type, "body": row.body,
        "has_media": bool(row.media_id), "media_url": f"/estates/marketing/social/whatsapp/media/{row.id}" if row.media_id else None,
        "status": row.status, "created_at": row.created_at, "read_by_staff_at": row.read_by_staff_at,
    }


@router.get("/{estate_id}/marketing/social/whatsapp/conversations")
def list_whatsapp_conversations(estate_id: int, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    return {"items": whatsapp_inbox.conversations_for_estate(db, estate.id)}


@router.get("/{estate_id}/marketing/social/whatsapp/conversations/{phone}/messages")
def get_whatsapp_thread(estate_id: int, phone: str, request: Request, db: Session = Depends(get_db)):
    estate, _access = _staff(db, request, estate_id)
    rows = whatsapp_inbox.thread(db, estate.id, phone)
    whatsapp_inbox.mark_read(db, estate.id, phone)
    db.commit()
    return {"items": [_message_payload(row) for row in rows], "can_reply_freely": whatsapp_inbox.can_reply_freely(db, estate.id, phone)}


class WhatsappReply(BaseModel):
    body: str = Field(min_length=1, max_length=4096)


@router.post("/{estate_id}/marketing/social/whatsapp/conversations/{phone}/reply")
def reply_whatsapp_conversation(estate_id: int, phone: str, payload: WhatsappReply, request: Request, db: Session = Depends(get_db)):
    estate, access = _staff(db, request, estate_id, permission=WRITE)
    if not social_whatsapp.configured():
        raise HTTPException(503, "WhatsApp messaging is not switched on for this server yet.")
    if not whatsapp_inbox.can_reply_freely(db, estate.id, phone):
        raise HTTPException(409, "It has been more than 24 hours since this customer last messaged - only a template message (from an opt-in broadcast) can reach them now, not a free reply.")
    rows = whatsapp_inbox.thread(db, estate.id, phone, limit=1)
    customer_id = rows[0].customer_id if rows else None
    try:
        row = whatsapp_inbox.send_reply(
            db, organization_id=estate.organization_id, estate_id=estate.id, phone_digits=phone, customer_id=customer_id,
            body=payload.body.strip(), actor_subject_type=access.principal.subject_type, actor_subject_id=str(access.principal.subject_id),
        )
    except social_whatsapp.WhatsAppError as exc:
        db.commit()
        raise HTTPException(502, str(exc)) from exc
    db.commit()
    return _message_payload(row)


@router.get("/marketing/social/whatsapp/media/{message_id}")
def get_whatsapp_media(message_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(EstateWhatsappMessage, message_id)
    if row is None or not row.media_id:
        raise HTTPException(404, "Media not found")
    require_estate_access(db, request, row.organization_id, permission="estate.read")
    try:
        data, mime_type = social_whatsapp.download_media(row.media_id)
    except social_whatsapp.WhatsAppError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(data, media_type=mime_type, headers=dict(NO_CACHE_PRIVATE))


@router.get("/{estate_id}/marketing/social/whatsapp/profile")
def get_whatsapp_profile(estate_id: int, request: Request, db: Session = Depends(get_db)):
    """Read-only: this is the one WhatsApp number shared by every LandCheck Estates company, so it
    isn't editable from an individual estate's dashboard - shown here only so staff can see what
    their customers see when a message arrives from it."""
    _staff(db, request, estate_id)
    if not social_whatsapp.configured():
        return {"configured": False}
    try:
        profile = social_whatsapp.get_business_profile()
    except social_whatsapp.WhatsAppError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"configured": True, **profile}


class CustomerWhatsappMessage(BaseModel):
    preset: str
    detail: str | None = Field(default=None, max_length=200)


@router.post("/customers/{customer_id}/whatsapp-message")
def send_customer_whatsapp_message(customer_id: int, payload: CustomerWhatsappMessage, request: Request, db: Session = Depends(get_db)):
    """A single, targeted template message to one customer - not a broadcast. Uses the same approved
    presets as the opt-in broadcast system, since only pre-approved templates can reliably reach
    someone who hasn't messaged LandCheck's WhatsApp number in the last 24 hours."""
    customer = db.get(EstateCustomer, customer_id)
    if customer is None:
        raise HTTPException(404, "Customer not found")
    access = require_estate_access(db, request, customer.organization_id, permission=WRITE)
    if not customer.phone:
        raise HTTPException(422, "This customer has no phone number on file.")
    if payload.preset not in social_whatsapp.PRESETS:
        raise HTTPException(422, "Unknown message preset")
    if not social_whatsapp.configured():
        raise HTTPException(503, "WhatsApp messaging is not switched on for this server yet.")
    digits = normalize_phone_digits(customer.phone)
    if not digits:
        raise HTTPException(422, "This customer's phone number is not valid.")
    allocation = db.query(EstateAllocation).filter(EstateAllocation.customer_id == customer.id).order_by(EstateAllocation.created_at.desc()).first()
    estate = db.get(Estate, allocation.estate_id) if allocation else None
    organization = db.get(EstateOrganization, customer.organization_id)
    params = [
        customer.full_name.split(" ")[0] if customer.full_name else "there",
        estate.name if estate else (organization.name if organization else "your estate"),
        payload.detail or "an update on your plot",
        public_page_url(estate) if estate else web_url(),
    ]
    try:
        message_id = social_whatsapp.send_template(digits, social_whatsapp.template_name(payload.preset), params)
    except social_whatsapp.WhatsAppError as exc:
        append_estate_audit_event(db, organization_id=customer.organization_id, actor=access.principal, action="customer.whatsapp_message_failed", entity_type="estate_customer", entity_id=customer.id, after_data={"preset": payload.preset, "error": str(exc)})
        db.commit()
        raise HTTPException(502, str(exc)) from exc
    if estate is not None:
        # Only logged to the inbox when there's an estate to attribute it to - the message itself
        # still sends either way, this just controls whether it shows up in a conversation thread.
        db.add(EstateWhatsappMessage(
            organization_id=customer.organization_id, estate_id=estate.id, customer_id=customer.id, phone_digits=digits,
            direction="out", message_type="text", body=f"[{social_whatsapp.PRESETS[payload.preset]['label']}] {payload.detail or ''}".strip(),
            wa_message_id=message_id or None, status="sent", sent_by_subject_type=access.principal.subject_type, sent_by_subject_id=str(access.principal.subject_id),
        ))
    append_estate_audit_event(db, organization_id=customer.organization_id, actor=access.principal, action="customer.whatsapp_message_sent", entity_type="estate_customer", entity_id=customer.id, after_data={"preset": payload.preset})
    db.commit()
    return {"ok": True}
