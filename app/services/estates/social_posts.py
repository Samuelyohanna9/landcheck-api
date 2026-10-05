from __future__ import annotations

"""Creating, rendering, publishing and reminding for estate marketing posts."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstateNotificationLog, EstatePlot
from app.models.estate_social import EstateSocialAccount, EstateSocialPlan, EstateSocialPost
from app.services.estates import marketing_render, social_meta
from app.services.estates.marketing_alerts import _dedupe, _member_emails, _send
from app.services.estates.marketing_common import api_url, web_url
from app.services.estates.social_templates import AUTOMATIC_CHANNELS, CHANNEL_FORMAT, CHANNEL_LABEL, MANUAL_CHANNELS, caption_for_channel
from app.utils.secret_box import SecretNotConfigured, decrypt_text, make_signed_token, read_signed_token

logger = logging.getLogger(__name__)

IMAGE_TOKEN_TTL_SECONDS = 24 * 3600
STALE_PLAN_POST = timedelta(hours=12)


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


def broadcast_image_url(estate_id: int, style: str) -> str:
    """A signed, expiring address for the flyer design attached to a WhatsApp broadcast's header."""
    token = make_signed_token("wai", estate_id, style, ttl_seconds=IMAGE_TOKEN_TTL_SECONDS)
    return f"{api_url()}/estates/marketing/social/whatsapp-image/{token}.png"


def read_broadcast_image_token(token: str) -> tuple[int, str] | None:
    parts = read_signed_token(token, "wai")
    if not parts or len(parts) != 2:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


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
    """When a company has connected several Pages (or several Instagram accounts), the one they marked
    default wins; otherwise falls back to the first connected, which is also what every organisation
    with only one connected account per provider already had - so this stays a no-op for them."""
    provider = "facebook" if channel == "facebook" else "instagram"
    return (
        db.query(EstateSocialAccount)
        .filter(EstateSocialAccount.organization_id == organization_id, EstateSocialAccount.provider == provider, EstateSocialAccount.status == "active")
        .order_by(EstateSocialAccount.is_default.desc(), EstateSocialAccount.id.asc())
        .first()
    )


# ── Delivery records (shown in Message delivery) ─────────────────────────────────────────────
def log_delivery(db: Session, post: EstateSocialPost, channel: str, outcome: dict[str, Any], *, account_name: str | None = None) -> None:
    """One Message delivery row per channel a post was sent to, so the company has a record of everything
    that went out (or failed, or was skipped) in the same place as its customer emails."""
    state = outcome.get("status")
    status = "sent" if state == "ok" else "skipped" if state == "skipped" else "failed"
    first_line = next((line.strip() for line in (post.caption or "").splitlines() if line.strip()), "Marketing post")
    db.add(EstateNotificationLog(
        organization_id=post.organization_id, estate_id=post.estate_id, channel=channel, event_key="social_post",
        recipient_name=(account_name or outcome.get("account") or CHANNEL_LABEL.get(channel, channel))[:255], subject=first_line[:255], status=status,
        error_message=(outcome.get("error") or None) if status != "sent" else None,
        details={
            "post_id": post.id, "plan_id": post.plan_id, "template": post.template_key, "url": outcome.get("url") or "",
            "manual": bool(outcome.get("manual")), "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else "",
            **({"meta_code": outcome.get("meta_code"), "meta_subcode": outcome.get("meta_subcode"), "meta_trace_id": outcome.get("meta_trace_id"), "image_url": outcome.get("image_url")} if status == "failed" else {}),
        },
        sent_at=datetime.now(timezone.utc) if status == "sent" else None,
    ))
    db.flush()



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
    want_media = post.include_media is not False
    if not want_media and channel != "facebook":
        # Instagram has no text-only post type - every post needs an image or video. This should
        # already be blocked at creation time (see _check_channels usage in the router), but stays
        # a clear failure here too rather than silently attempting it with a missing image.
        return {"status": "failed", "error": f"{CHANNEL_LABEL[channel]} requires an image - turn media back on for this post, or remove {CHANNEL_LABEL[channel]} from its channels."}
    if want_media:
        # Rendering is CPU-heavy and can wait on a satellite-tile fetch (see cached_render's own
        # comment) - the first request for a given post/format/style combination can take several
        # seconds. Facebook/Instagram's own fetch of the image URL times out far sooner than that, so
        # a cold render there reads to them as "no usable image" even though the image is perfectly
        # fine - it just wasn't ready in time. Rendering it here first, synchronously, before Facebook
        # is ever asked to fetch it, means their fetch always hits the warm cache_render cache instead.
        estate = db.get(Estate, post.estate_id)
        if estate is not None:
            try:
                render_image(db, estate, fmt=CHANNEL_FORMAT.get(channel, "post"), style=post.image_style, plot_id=post.plot_id, source=post.source_code)
            except Exception:
                logger.exception("Pre-render before publish failed (post=%s, channel=%s)", post.id, channel)
    image_url = post_image_url(post, channel) if want_media else None
    account_id, account_name = account.external_id, account.name
    db.commit()  # release the connection during the network calls below
    try:
        if channel == "facebook":
            outcome = social_meta.publish_facebook_photo(account_id, token, image_url, caption) if want_media else social_meta.publish_facebook_text(account_id, token, caption)
        else:
            outcome = social_meta.publish_instagram_image(account_id, token, image_url, caption, story=channel == "instagram_story")
    except social_meta.MetaError as exc:
        if exc.needs_reconnect:
            fresh = db.get(EstateSocialAccount, account.id)
            if fresh is not None:
                fresh.status = "needs_reconnect"
        return {
            "status": "failed", "error": str(exc), "needs_reconnect": exc.needs_reconnect, "account": account_name,
            "meta_code": exc.code, "meta_subcode": exc.subcode, "meta_trace_id": exc.trace_id, "image_url": image_url,
        }
    except Exception as exc:  # network trouble etc.
        logger.exception("Social publish failed (post=%s, channel=%s)", post.id, channel)
        return {"status": "failed", "error": f"Could not reach {CHANNEL_LABEL[channel]}: {exc}", "account": account_name, "image_url": image_url}
    return {"status": "ok", "external_id": outcome.get("external_id"), "url": outcome.get("url"), "account": account_name, "done_at": datetime.now(timezone.utc).isoformat()}


def publish_post(db: Session, post: EstateSocialPost) -> EstateSocialPost:
    """Send the post to its automatic channels. Channels that already succeeded are never sent twice, so
    retrying after a partial failure is safe. Manual channels stay 'pending' until someone marks them posted."""
    if post.auto_caption:
        from app.services.estates.social_planner import refresh_auto_post

        if not refresh_auto_post(db, post):
            post.status = "skipped"
            post.results = {"skipped": {"status": "skipped", "error": "No plots are available right now, so nothing was posted."}}
            for channel in channels_of(post):
                log_delivery(db, post, channel, {"status": "skipped", "error": "No plots are available right now, so nothing was posted."})
            db.commit()
            return post
        db.commit()
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
        log_delivery(db, post, channel, outcome)
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
    log_delivery(db, post, channel, results[channel])
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
    # A plan post that is many hours late (server down, plan resumed) would go out at a strange time with
    # stale timing - skip it instead; the plan keeps going with its later posts.
    stale_ids = db.execute(text("""
        UPDATE estate_social_posts SET status = 'skipped', updated_at = NOW(),
               results = '{"skipped": {"status": "skipped", "error": "The scheduled time passed before it could be posted."}}'::json
        WHERE status = 'scheduled' AND plan_id IS NOT NULL AND scheduled_at < :stale
        RETURNING id
    """), {"stale": now - STALE_PLAN_POST}).scalars().all()
    for stale_id in stale_ids:
        stale = db.get(EstateSocialPost, stale_id)
        if stale is not None:
            for channel in channels_of(stale):
                log_delivery(db, stale, channel, {"status": "skipped", "error": "The scheduled time passed before it could be posted."})
    # A worker that died mid-publish leaves the post 'publishing' forever; give it a clear failed state.
    db.execute(text("""
        UPDATE estate_social_posts SET status = 'failed', updated_at = NOW()
        WHERE status = 'publishing' AND updated_at < :stuck
    """), {"stuck": now - timedelta(minutes=20)})
    claimed = db.execute(text("""
        UPDATE estate_social_posts SET status = 'publishing', updated_at = NOW()
        WHERE id IN (
            SELECT p.id FROM estate_social_posts p LEFT JOIN estate_social_plans pl ON pl.id = p.plan_id
            WHERE p.status = 'scheduled' AND p.scheduled_at IS NOT NULL AND p.scheduled_at <= :now
              AND (p.plan_id IS NULL OR pl.status = 'active')
              AND p.channels::jsonb ?| array['facebook', 'instagram', 'instagram_story']
            ORDER BY p.scheduled_at ASC LIMIT 10 FOR UPDATE OF p SKIP LOCKED)
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
        if post.plan_id:
            plan = db.get(EstateSocialPlan, post.plan_id)
            if plan is not None and plan.status != "active":
                continue
        if post.auto_caption:
            from app.services.estates.social_planner import refresh_auto_post

            if not refresh_auto_post(db, post):
                post.status = "skipped"
                post.results = {"skipped": {"status": "skipped", "error": "No plots are available right now, so nothing was posted."}}
                post.reminder_sent_at = now
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
    from app.services.estates.social_planner import extend_active_plans

    publish_due_posts(db)
    send_manual_post_reminders(db)
    extend_active_plans(db)
    from app.services.estates.social_broadcast import process_queued_whatsapp_sends

    process_queued_whatsapp_sends(db)


def account_public(account: EstateSocialAccount) -> dict[str, Any]:
    """Never includes the token."""
    return {"id": account.id, "provider": account.provider, "name": account.name, "username": account.username, "status": account.status, "linked_page_id": account.linked_page_id, "is_default": account.is_default}
