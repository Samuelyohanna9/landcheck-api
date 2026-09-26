from __future__ import annotations

"""Creating, rendering, publishing and reminding for estate marketing posts."""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstatePlot
from app.models.estate_social import EstateSocialAccount, EstateSocialPost
from app.services.estates import marketing_render, social_meta
from app.services.estates.marketing_alerts import _dedupe, _member_emails, _send
from app.services.estates.marketing_common import api_url, web_url
from app.services.estates.social_templates import AUTOMATIC_CHANNELS, CHANNEL_FORMAT, CHANNEL_LABEL, MANUAL_CHANNELS, caption_for_channel
from app.utils.secret_box import SecretNotConfigured, decrypt_text, make_signed_token, read_signed_token

logger = logging.getLogger(__name__)

IMAGE_TOKEN_TTL_SECONDS = 24 * 3600


# ── Images ───────────────────────────────────────────────────────────────────────────────────
def render_image(db: Session, estate: Estate, *, fmt: str, style: str, plot_id: int | None, source: str | None) -> bytes:
    """The post image. QR codes are left off: on a phone screenshot they cannot be scanned, and the
    caption already carries the tracked link."""
    ctx = marketing_render.build_context(db, estate, source=source)
    plot = next((item for item in ctx.plots if item.id == plot_id), None) if plot_id else None
    key = ("social-image", estate.id, fmt, style, plot.id if plot else None, plot.commercial_status if plot else None, marketing_render.context_signature(ctx))
    if plot is not None:
        return marketing_render.cached_render(key, 120, lambda: marketing_render.compose_plot_ad(ctx, plot, fmt, qr=False, style=style))
    return marketing_render.cached_render(key, 120, lambda: marketing_render.compose_estate_ad(ctx, fmt, qr=False, style=style))


def post_image_url(post: EstateSocialPost, channel: str) -> str:
    """A signed, expiring public address Meta can fetch the image from (their servers cannot log in)."""
    fmt = CHANNEL_FORMAT.get(channel, "post")
    token = make_signed_token("img", post.id, fmt, ttl_seconds=IMAGE_TOKEN_TTL_SECONDS)
    return f"{api_url()}/estates/marketing/social/image/{token}.png"


def read_image_token(token: str) -> tuple[int, str] | None:
    parts = read_signed_token(token, "img")
    if not parts or len(parts) != 2:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


# ── Publishing ───────────────────────────────────────────────────────────────────────────────
def _account_for(db: Session, organization_id: int, channel: str) -> EstateSocialAccount | None:
    provider = "facebook" if channel == "facebook" else "instagram"
    return (
        db.query(EstateSocialAccount)
        .filter(EstateSocialAccount.organization_id == organization_id, EstateSocialAccount.provider == provider, EstateSocialAccount.status == "active")
        .order_by(EstateSocialAccount.id.asc())
        .first()
    )


def channels_of(post: EstateSocialPost) -> list[str]:
    return [channel for channel in (post.channels or []) if channel in CHANNEL_LABEL]


def _publish_channel(db: Session, post: EstateSocialPost, channel: str) -> dict[str, Any]:
    account = _account_for(db, post.organization_id, channel)
    if account is None:
        return {"status": "failed", "error": f"No {CHANNEL_LABEL[channel]} account is connected. Connect it in the Social posts tab."}
    try:
        token = decrypt_text(account.access_token_enc)
    except SecretNotConfigured as exc:
        return {"status": "failed", "error": str(exc)}
    caption = caption_for_channel(post.caption, channel)
    image_url = post_image_url(post, channel)
    account_id, account_name = account.external_id, account.name
    db.commit()  # release the connection during the network calls below
    try:
        if channel == "facebook":
            outcome = social_meta.publish_facebook_photo(account_id, token, image_url, caption)
        else:
            outcome = social_meta.publish_instagram_image(account_id, token, image_url, caption, story=channel == "instagram_story")
    except social_meta.MetaError as exc:
        if exc.needs_reconnect:
            fresh = db.get(EstateSocialAccount, account.id)
            if fresh is not None:
                fresh.status = "needs_reconnect"
        return {"status": "failed", "error": str(exc), "needs_reconnect": exc.needs_reconnect}
    except Exception as exc:  # network trouble etc.
        logger.exception("Social publish failed (post=%s, channel=%s)", post.id, channel)
        return {"status": "failed", "error": f"Could not reach {CHANNEL_LABEL[channel]}: {exc}"}
    return {"status": "ok", "external_id": outcome.get("external_id"), "url": outcome.get("url"), "account": account_name, "done_at": datetime.now(timezone.utc).isoformat()}


def publish_post(db: Session, post: EstateSocialPost) -> EstateSocialPost:
    """Send the post to its automatic channels. Channels that already succeeded are never sent twice, so
    retrying after a partial failure is safe. Manual channels stay 'pending' until someone marks them posted."""
    results = dict(post.results or {})
    auto = [channel for channel in channels_of(post) if channel in AUTOMATIC_CHANNELS]
    for channel in channels_of(post):
        if channel in MANUAL_CHANNELS and channel not in results:
            results[channel] = {"status": "pending"}
    post.status = "publishing"
    post.results = results
    db.commit()

    for channel in auto:
        if (results.get(channel) or {}).get("status") == "ok":
            continue
        outcome = _publish_channel(db, post, channel)
        results = dict(post.results or {})
        results[channel] = outcome
        post.results = results
        db.commit()

    statuses = [(post.results or {}).get(channel, {}).get("status") for channel in auto]
    if not auto:
        post.status = "scheduled" if post.scheduled_at else "draft"
    elif all(status == "ok" for status in statuses):
        post.status = "published"
        post.published_at = datetime.now(timezone.utc)
    elif any(status == "ok" for status in statuses):
        post.status = "partial"
        post.published_at = datetime.now(timezone.utc)
    else:
        post.status = "failed"
    db.commit()
    return post


def mark_manual_posted(db: Session, post: EstateSocialPost, channel: str) -> EstateSocialPost:
    if channel not in MANUAL_CHANNELS or channel not in channels_of(post):
        raise ValueError("That channel is not a manual channel of this post")
    results = dict(post.results or {})
    results[channel] = {"status": "ok", "done_at": datetime.now(timezone.utc).isoformat(), "manual": True}
    post.results = results
    manual_left = [c for c in channels_of(post) if c in MANUAL_CHANNELS and (post.results.get(c) or {}).get("status") != "ok"]
    auto = [c for c in channels_of(post) if c in AUTOMATIC_CHANNELS]
    auto_ok = all((post.results.get(c) or {}).get("status") == "ok" for c in auto)
    if not manual_left and auto_ok:
        post.status = "published"
        post.published_at = post.published_at or datetime.now(timezone.utc)
    db.commit()
    return post


# ── Scheduler sweeps ─────────────────────────────────────────────────────────────────────────
def publish_due_posts(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Called every minute: publish scheduled posts whose time has come."""
    now = now or datetime.now(timezone.utc)
    claimed = db.execute(text("""
        UPDATE estate_social_posts SET status = 'publishing', updated_at = NOW()
        WHERE id IN (
            SELECT id FROM estate_social_posts
            WHERE status = 'scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= :now
              AND channels::jsonb ?| array['facebook', 'instagram', 'instagram_story']
            ORDER BY scheduled_at ASC LIMIT 10 FOR UPDATE SKIP LOCKED)
        RETURNING id
    """), {"now": now}).scalars().all()
    db.commit()
    done = 0
    for post_id in claimed:
        post = db.get(EstateSocialPost, post_id)
        if post is None:
            continue
        try:
            publish_post(db, post)
            done += 1
        except Exception:
            logger.exception("Scheduled social post %s failed", post_id)
            db.rollback()
            failed = db.get(EstateSocialPost, post_id)
            if failed is not None and failed.status == "publishing":
                failed.status = "failed"
                failed.results = {**(failed.results or {}), "error": {"status": "failed", "error": "Unexpected error while publishing"}}
                db.commit()
    return {"published": done}


def send_manual_post_reminders(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """When a post that needs a manual step (WhatsApp Status) reaches its time, email the team."""
    now = now or datetime.now(timezone.utc)
    posts = (
        db.query(EstateSocialPost)
        .filter(EstateSocialPost.scheduled_at.isnot(None), EstateSocialPost.scheduled_at <= now, EstateSocialPost.reminder_sent_at.is_(None), EstateSocialPost.status.in_(("scheduled", "published", "partial")))
        .order_by(EstateSocialPost.scheduled_at.asc())
        .limit(50)
        .all()
    )
    sent = 0
    for post in posts:
        manual = [c for c in channels_of(post) if c in MANUAL_CHANNELS and (post.results or {}).get(c, {}).get("status") != "ok"]
        if not manual:
            post.reminder_sent_at = now
            continue
        estate = db.get(Estate, post.estate_id)
        if estate is None:
            continue
        recipients = _dedupe(_member_emails(db, post.organization_id, ("owner", "manager", "marketer")))
        labels = ", ".join(CHANNEL_LABEL[c] for c in manual)
        link = f"{web_url()}/estates/{estate.id}/marketing?tab=social&post={post.id}"
        first_line = (post.caption or "").strip().splitlines()[0][:120] if (post.caption or "").strip() else ""
        for email in recipients:
            if _send(email, f"Time to post: {estate.name} on {labels}", "Your scheduled post is ready", f"<p><strong>{estate.name}</strong> - {first_line}</p><p>Post it to <strong>{labels}</strong>. Open the post, tap <em>Share to Status</em>, then mark it as posted.</p>", [("Open the post", link)]):
                sent += 1
        post.reminder_sent_at = now
    db.commit()
    return {"reminders": sent}


def run_social_sweeps(db: Session) -> None:
    publish_due_posts(db)
    send_manual_post_reminders(db)
    from app.services.estates.social_broadcast import process_queued_whatsapp_sends

    process_queued_whatsapp_sends(db)


def account_public(account: EstateSocialAccount) -> dict[str, Any]:
    """Never includes the token."""
    return {"id": account.id, "provider": account.provider, "name": account.name, "username": account.username, "status": account.status, "linked_page_id": account.linked_page_id}
