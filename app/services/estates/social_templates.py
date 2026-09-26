from __future__ import annotations

"""Ready-made captions for estate marketing posts, filled from live estate data.

Every template returns plain text that staff can edit before posting. Links use the estate's tracked
share link so enquiries and sales are attributed to the post."""

import re
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.estate_foundation import EstatePlot
from app.models.estate_marketing import EstateInspectionSlot, EstateProgressUpdate
from app.services.estates.marketing_common import share_page_url
from app.services.estates.marketing_render import MarketingContext, area_text, naira, naira_short

LAGOS = ZoneInfo("Africa/Lagos")
URL_PATTERN = re.compile(r"https?://\S+")

# Channel -> the image shape it needs. Feed posts are 4:5, stories and WhatsApp Status are 9:16.
CHANNEL_FORMAT = {"facebook": "post", "instagram": "post", "instagram_story": "status", "whatsapp_status": "status", "other": "post"}
AUTOMATIC_CHANNELS = ("facebook", "instagram", "instagram_story")
MANUAL_CHANNELS = ("whatsapp_status", "other")
ALL_CHANNELS = AUTOMATIC_CHANNELS + MANUAL_CHANNELS
CHANNEL_LABEL = {
    "facebook": "Facebook Page",
    "instagram": "Instagram post",
    "instagram_story": "Instagram story",
    "whatsapp_status": "WhatsApp Status",
    "other": "Other (post it yourself)",
}


def _price_from(ctx: MarketingContext) -> str | None:
    return naira_short(ctx.min_price) if (ctx.show_prices and ctx.min_price) else None


def _plan_line(ctx: MarketingContext) -> str | None:
    if not ctx.payment_plan:
        return None
    parts = [f"{item.get('percentage')}% {str(item.get('label') or '').lower()}" for item in ctx.payment_plan if item.get("percentage")]
    return "Pay in stages: " + " · ".join(parts) if parts else None


def _hashtags(ctx: MarketingContext) -> str:
    tags = ["#LandForSale", "#RealEstateNigeria", "#Estate"]
    location = str(ctx.location or "")
    for chunk in re.split(r"[,/-]", location)[:2]:
        word = re.sub(r"[^A-Za-z0-9]", "", chunk.title())
        if 3 <= len(word) <= 24:
            tags.insert(0, f"#{word}")
    return " ".join(dict.fromkeys(tags))


def _link(ctx: MarketingContext, plot: EstatePlot | None = None) -> str:
    return share_page_url(ctx.estate, source=ctx.source, plot_id=plot.id if plot else None)


def _next_slot(db: Session, ctx: MarketingContext) -> EstateInspectionSlot | None:
    return (
        db.query(EstateInspectionSlot)
        .filter(EstateInspectionSlot.estate_id == ctx.estate.id, EstateInspectionSlot.status == "open", EstateInspectionSlot.starts_at >= datetime.now(timezone.utc))
        .order_by(EstateInspectionSlot.starts_at.asc())
        .first()
    )


def _latest_progress(db: Session, ctx: MarketingContext) -> EstateProgressUpdate | None:
    return (
        db.query(EstateProgressUpdate)
        .filter(EstateProgressUpdate.estate_id == ctx.estate.id, EstateProgressUpdate.is_published.is_(True))
        .order_by(EstateProgressUpdate.created_at.desc())
        .first()
    )


def _lines(*items: str | None) -> str:
    return "\n".join(item for item in items if item)


def build_templates(db: Session, ctx: MarketingContext, plot: EstatePlot | None = None) -> list[dict]:
    """The caption templates that make sense for this estate right now (each one only appears when the
    data behind it exists, so nobody posts an empty 'inspection' announcement)."""
    name = ctx.estate.name
    where = f"📍 {ctx.location}" if ctx.location else None
    price = _price_from(ctx)
    link = _link(ctx)
    tags = _hashtags(ctx)
    templates: list[dict] = []

    templates.append({
        "key": "new_plots", "label": "Plots available", "hint": "Announce that plots are on sale",
        "caption": _lines(
            f"🌿 {name} — plots now available",
            where,
            f"{ctx.available} plots open" + (f", from {price}" if price else "") + ".",
            _plan_line(ctx),
            "",
            f"See the live map and reserve online: {link}",
            "",
            tags,
        ),
    })
    if 0 < ctx.available <= 25:
        templates.append({
            "key": "few_left", "label": "Only a few left", "hint": "Create urgency when stock is low",
            "caption": _lines(
                f"⏳ Only {ctx.available} plot{'s' if ctx.available != 1 else ''} left at {name}",
                where,
                (f"From {price}." if price else None),
                _plan_line(ctx),
                "",
                f"Reserve yours before they are gone: {link}",
                "",
                tags,
            ),
        })
    if ctx.sold >= 3:
        templates.append({
            "key": "social_proof", "label": "Plots already sold", "hint": "Show that buyers are moving",
            "caption": _lines(
                f"✅ {ctx.sold} plots already taken at {name}",
                where,
                f"{ctx.available} still available" + (f" from {price}" if price else "") + ".",
                "",
                f"Join them: {link}",
                "",
                tags,
            ),
        })
    if price and ctx.payment_plan:
        templates.append({
            "key": "payment_plan", "label": "Payment plan", "hint": "Lead with affordability",
            "caption": _lines(
                f"💳 Own land at {name} without paying all at once",
                _plan_line(ctx),
                f"Plots from {price}.",
                where,
                "",
                f"Check plots and prices: {link}",
                "",
                tags,
            ),
        })
    slot = _next_slot(db, ctx)
    if slot is not None:
        when = slot.starts_at.astimezone(LAGOS).strftime("%A, %d %B at %I:%M %p").replace(" 0", " ")
        meeting = (ctx.estate.public_meeting_point or {}) if hasattr(ctx.estate, "public_meeting_point") else {}
        meeting_line = f"Meeting point: {meeting.get('label') or meeting.get('address')}" if isinstance(meeting, dict) and (meeting.get("label") or meeting.get("address")) else None
        templates.append({
            "key": "inspection", "label": "Site inspection", "hint": "Invite people to the next site visit",
            "caption": _lines(
                f"🗓 Site inspection — {name}",
                f"{when}",
                meeting_line,
                where,
                "",
                f"Book your place: {link}",
                "",
                tags,
            ),
        })
    update = _latest_progress(db, ctx)
    if update is not None:
        templates.append({
            "key": "progress", "label": "Progress update", "hint": "Build trust with the latest site update",
            "caption": _lines(
                f"🏗 Update from {name}",
                update.title,
                (update.body[:280] if getattr(update, "body", None) else None),
                where,
                "",
                f"See more: {link}",
                "",
                tags,
            ),
        })
    if plot is not None:
        plot_price = naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else None
        templates.insert(0, {
            "key": "plot_spotlight", "label": "This plot", "hint": "Feature one plot",
            "caption": _lines(
                f"📌 Plot {plot.plot_number} at {name}",
                area_text(plot.area_sqm),
                plot_price,
                where,
                "",
                f"View this plot and reserve: {_link(ctx, plot)}",
                "",
                tags,
            ),
        })
    templates.append({"key": "custom", "label": "Write my own", "hint": "Start from a blank caption", "caption": _lines("", "", f"{link}")})
    return templates


def caption_for_channel(caption: str, channel: str) -> str:
    """Instagram does not make links clickable, so replace them with a pointer to the profile link."""
    text = str(caption or "").strip()
    if channel in ("instagram", "instagram_story"):
        had_link = bool(URL_PATTERN.search(text))
        text = URL_PATTERN.sub("", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if had_link and "link in bio" not in text.lower():
            text += "\n\n🔗 Link in bio to view plots and reserve."
    return text
