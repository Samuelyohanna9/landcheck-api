from __future__ import annotations

"""Ready-made marketing captions for an estate, written from live data.

Each template is one persuasion angle (announcement, choice, affordability, scarcity, proof, trust, progress,
location outlook, education, objection handling). Every template has several wordings, and a tone
(friendly, professional, urgent) that changes the call to action. Claims always come from real data:
"only 5 plots left" appears only when five plots really are left, and a template that has no data
behind it (no inspection date, no progress update) is simply not offered."""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.estate_foundation import EstatePlot
from app.models.estate_marketing import EstateInspectionSlot, EstateProgressUpdate
from app.services.estates.marketing_common import share_page_url, whatsapp_link
from app.services.estates.marketing_render import MarketingContext, area_text, naira, naira_short

LAGOS = ZoneInfo("Africa/Lagos")
URL_PATTERN = re.compile(r"https?://\S+")
EMOJI_PATTERN = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⬆⏳⏰✅✔✨\U0001F1E6-\U0001F1FF]️?")
TONES = ("friendly", "professional", "urgent")
VARIANTS = 3

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

# key -> (label, strategy, hint)
TEMPLATE_INFO = {
    "new_plots": ("Plots available", "Announcement", "Announce that plots are on sale"),
    "size_options": ("Sizes and prices", "Choice", "Show the plot sizes and what each costs"),
    "payment_plan": ("Payment plan", "Affordability", "Lead with how buyers can pay"),
    "plot_spotlight": ("Featured plot", "Feature", "Put one plot in the spotlight"),
    "how_to_buy": ("How to buy", "Trust", "Explain the steps from choosing to owning"),
    "progress": ("Progress update", "Progress", "Share the latest update from site"),
    "location_outlook": ("Area outlook", "Location", "Show where development is moving"),
    "few_left": ("Only a few left", "Scarcity", "Create real urgency when stock is low"),
    "social_proof": ("Plots already sold", "Proof", "Show that buyers are moving"),
    "inspection": ("Site inspection", "Invitation", "Invite people to the next site visit"),
    "faq": ("Common question", "Objections", "Answer a question buyers hesitate over"),
    "why_land": ("Buying land wisely", "Education", "Give buyers a checklist they will trust you for"),
    "contact_cta": ("Talk to us", "Conversation", "Invite questions on WhatsApp"),
}
# The rotation order: varied angles so two similar posts never sit side by side.
BASE_ORDER = ["new_plots", "size_options", "payment_plan", "plot_spotlight", "how_to_buy", "progress", "location_outlook", "few_left", "social_proof", "inspection", "faq", "why_land", "contact_cta"]


# ── Small helpers ────────────────────────────────────────────────────────────────────────────
def _price_from(ctx: MarketingContext) -> str | None:
    return naira_short(ctx.min_price) if (ctx.show_prices and ctx.min_price) else None


def _plan_line(ctx: MarketingContext) -> str | None:
    if not ctx.payment_plan:
        return None
    parts = [f"{item.get('percentage')}% {str(item.get('label') or '').lower()}" for item in ctx.payment_plan if item.get("percentage")]
    return "Pay in stages: " + " · ".join(parts) if parts else None


def _plan_parts(ctx: MarketingContext) -> str | None:
    if not ctx.payment_plan:
        return None
    parts = [f"{item.get('percentage')}% {str(item.get('label') or '').lower()}" for item in ctx.payment_plan if item.get("percentage")]
    return " · ".join(parts) if parts else None


def _is_scarce(ctx: MarketingContext) -> bool:
    """Real scarcity only: few plots left in absolute terms AND a small share of the estate."""
    total = len(ctx.plots)
    return 0 < ctx.available <= 25 and (total == 0 or ctx.available <= max(3, 0.35 * total))


def _city(ctx: MarketingContext) -> str | None:
    if not ctx.location:
        return None
    first = re.split(r"[,/-]", str(ctx.location))[0].strip()
    return first if 2 < len(first) <= 30 else None


def _hashtags(ctx: MarketingContext) -> str:
    tags = ["#LandForSale", "#RealEstateNigeria", "#Estate"]
    for chunk in re.split(r"[,/-]", str(ctx.location or ""))[:2]:
        word = re.sub(r"[^A-Za-z0-9]", "", chunk.title())
        if 3 <= len(word) <= 24:
            tags.insert(0, f"#{word}")
    return " ".join(dict.fromkeys(tags))


def _link(ctx: MarketingContext, plot: EstatePlot | None = None) -> str:
    return share_page_url(ctx.estate, source=ctx.source, plot_id=plot.id if plot else None)


def _cta(ctx: MarketingContext, tone: str, plot: EstatePlot | None = None) -> str:
    link = _link(ctx, plot)
    if tone == "urgent":
        return f"👉 Secure your plot today: {link}"
    if tone == "professional":
        return f"View availability and reserve online: {link}"
    return f"👉 See the map and reserve: {link}"


def _lines(*items: str | None) -> str:
    return "\n".join(item for item in items if item is not None)


def _finish(text: str, ctx: MarketingContext, tone: str) -> str:
    """Tidy blank lines; the professional tone drops emoji."""
    if tone == "professional":
        text = re.sub(r"([0-9])\uFE0F?\u20E3", r"\1.", text)
        text = EMOJI_PATTERN.sub("", text)
        text = re.sub(r"^[ \t]+", "", text, flags=re.M)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _size_groups(ctx: MarketingContext) -> list[tuple[int, Decimal | None, int]]:
    groups: dict[int, list[EstatePlot]] = {}
    for plot in ctx.available_plots:
        if plot.area_sqm:
            groups.setdefault(int(round(float(plot.area_sqm))), []).append(plot)
    result = []
    for area in sorted(groups):
        prices = [Decimal(str(p.asking_price)) for p in groups[area] if p.asking_price is not None]
        result.append((area, min(prices) if (prices and ctx.show_prices) else None, len(groups[area])))
    return result


def _next_slot(db: Session, ctx: MarketingContext) -> EstateInspectionSlot | None:
    return (
        db.query(EstateInspectionSlot)
        .filter(EstateInspectionSlot.estate_id == ctx.estate.id, EstateInspectionSlot.status == "open", EstateInspectionSlot.starts_at >= datetime.now(timezone.utc) + timedelta(hours=2))
        .order_by(EstateInspectionSlot.starts_at.asc())
        .first()
    )


def _latest_progress(db: Session, ctx: MarketingContext) -> EstateProgressUpdate | None:
    return (
        db.query(EstateProgressUpdate)
        .filter(EstateProgressUpdate.estate_id == ctx.estate.id, EstateProgressUpdate.is_published.is_(True), EstateProgressUpdate.created_at >= datetime.now(timezone.utc) - timedelta(days=90))
        .order_by(EstateProgressUpdate.created_at.desc())
        .first()
    )


def _when(slot: EstateInspectionSlot) -> str:
    return slot.starts_at.astimezone(LAGOS).strftime("%A, %d %B at %I:%M %p").replace(" 0", " ")


def _meeting_line(ctx: MarketingContext) -> str | None:
    meeting = getattr(ctx.estate, "public_meeting_point", None)
    if isinstance(meeting, dict):
        label = meeting.get("label") or meeting.get("address")
        if label:
            return f"Meeting point: {label}"
    return None


def _outlook(ctx: MarketingContext) -> dict | None:
    forecast = ctx.forecast
    if not (isinstance(forecast, dict) and forecast.get("data_available")):
        return None
    from app.services.estates.marketing_pdf import _potential_label

    growth = forecast.get("growth") or {}
    return {"label": _potential_label(forecast), "direction": growth.get("direction"), "distance": growth.get("frontier_distance_m"), "rate": growth.get("annual_percent_rate")}


# ── Applicability ────────────────────────────────────────────────────────────────────────────
def applicable_keys(db: Session, ctx: MarketingContext) -> list[str]:
    """The templates that have real data behind them right now, in rotation order."""
    if ctx.available <= 0:
        return []
    sizes = _size_groups(ctx)
    checks = {
        "new_plots": True,
        "size_options": bool(sizes) and any(price for _a, price, _c in sizes),
        "payment_plan": bool(ctx.payment_plan) and bool(_price_from(ctx)),
        "plot_spotlight": bool(ctx.available_plots),
        "how_to_buy": True,
        "progress": _latest_progress(db, ctx) is not None,
        "location_outlook": _outlook(ctx) is not None,
        "few_left": _is_scarce(ctx),
        "social_proof": ctx.sold >= 3,
        "inspection": _next_slot(db, ctx) is not None,
        "faq": True,
        "why_land": True,
        "contact_cta": bool(ctx.whatsapp_digits or ctx.contact_phone),
    }
    return [key for key in BASE_ORDER if checks.get(key)]


# ── Rendering ────────────────────────────────────────────────────────────────────────────────
def render(key: str, db: Session, ctx: MarketingContext, *, variant: int = 0, tone: str = "friendly", plot: EstatePlot | None = None) -> str | None:
    """The caption for one template and wording, or None when the data for it is missing."""
    variant = int(variant) % VARIANTS
    tone = tone if tone in TONES else "friendly"
    name = ctx.estate.name
    where = f"📍 {ctx.location}" if ctx.location else None
    price = _price_from(ctx)
    plan = _plan_line(ctx)
    city = _city(ctx)
    n = ctx.available
    cta = _cta(ctx, tone)
    tags = _hashtags(ctx)
    text: str | None = None

    if key == "new_plots":
        if variant == 0:
            text = _lines(f"🌿 {name} — plots now available", where, f"{n} plots open" + (f", from {price}" if price else "") + ".", plan, "", cta, "", tags)
        elif variant == 1:
            text = _lines(f"Looking for land{f' in {city}' if city else ''}? {name} has {n} plots ready.", (f"Starting from {price}." if price else None), "✔ See every plot on a live satellite map", "✔ Reserve online in minutes", plan, "", cta, "", tags)
        else:
            text = _lines(f"📢 Now selling: {name}", where, "Choose your plot, book a site visit, reserve online.", (f"Plots from {price}." if price else None), plan, "", cta, "", tags)

    elif key == "size_options":
        sizes = _size_groups(ctx)
        if not sizes:
            return None
        rows = [f"• {area:,} sqm" + (f" — from {naira_short(price)}" if price else "") + f" ({count} available)" for area, price, count in sizes[:4]]
        heads = [f"📐 Which plot size fits your plan? — {name}", f"Pick the size that suits your budget at {name}", f"Sizes and prices at {name}"]
        text = _lines(heads[variant], where, "", *rows, "", plan, "", cta, "", tags)

    elif key == "payment_plan":
        if not (plan and price):
            return None
        if variant == 0:
            text = _lines(f"💳 Own land at {name} without paying it all at once", plan, f"Plots from {price}.", where, "", cta, "", tags)
        elif variant == 1:
            text = _lines("Not ready to pay in full? You don't have to.", f"At {name} you can pay in stages: {_plan_parts(ctx)}.", f"Plots from {price}.", "", cta, "", tags)
        else:
            text = _lines(f"Spread the cost of your land — {name}", plan, f"From {price} per plot.", where, "", cta, "", tags)

    elif key == "few_left":
        if not _is_scarce(ctx):
            return None
        s = "" if n == 1 else "s"
        if variant == 0:
            text = _lines(f"⏳ Only {n} plot{s} left at {name}", where, (f"From {price}." if price else None), plan, "", cta, "", tags)
        elif variant == 1:
            text = _lines(f"Running low: just {n} plot{s} remain{'s' if n == 1 else ''} at {name}.", where, "When they are gone, they are gone.", "", cta, "", tags)
        else:
            text = _lines(f"{ctx.sold} plots taken. {n} left." if ctx.sold else f"{n} plots left.", f"{name}", where, "", cta, "", tags)

    elif key == "social_proof":
        if ctx.sold < 3:
            return None
        if variant == 0:
            text = _lines(f"✅ {ctx.sold} plots already taken at {name}", where, f"{n} still available" + (f" from {price}" if price else "") + ".", "", cta, "", tags)
        elif variant == 1:
            text = _lines(f"Buyers are choosing {name}.", f"{ctx.sold} plots sold, {n} open.", where, "", cta, "", tags)
        else:
            text = _lines(f"Join the {ctx.sold} buyers who have already secured land at {name}.", (f"Plots from {price}." if price else None), "", cta, "", tags)

    elif key == "inspection":
        slot = _next_slot(db, ctx)
        if slot is None:
            return None
        when = _when(slot)
        meeting = _meeting_line(ctx)
        if variant == 0:
            text = _lines(f"🗓 Site inspection — {name}", when, meeting, where, "", f"Book your place: {_link(ctx)}", "", tags)
        elif variant == 1:
            text = _lines("Don't buy land you haven't seen.", f"Join our site inspection at {name} — {when}.", meeting, "", f"Book your place: {_link(ctx)}", "", tags)
        else:
            text = _lines(f"See {name} for yourself", when, meeting, "Walk the land, meet the team, ask your questions.", "", f"Reserve your place: {_link(ctx)}", "", tags)

    elif key == "progress":
        update = _latest_progress(db, ctx)
        if update is None:
            return None
        body = (update.body or "").strip()
        body = body[:260] + ("..." if len(body) > 260 else "")
        if variant == 0:
            text = _lines(f"🏗 Update from {name}", update.title, (body or None), where, "", f"See more: {_link(ctx)}", "", tags)
        elif variant == 1:
            text = _lines(f"Development update: {update.title}", (body or None), f"— {name}", "", f"See more: {_link(ctx)}", "", tags)
        else:
            text = _lines(f"Here is what is happening on site at {name}:", update.title, (body or None), "", f"Visit the page for photos and plots: {_link(ctx)}", "", tags)

    elif key == "location_outlook":
        info = _outlook(ctx)
        if info is None:
            return None
        direction = f"Built-up land around the estate has been moving {str(info['direction']).lower()}." if info.get("direction") and str(info["direction"]).lower() != "no clear direction" else None
        distance = None
        try:
            metres = float(info["distance"]) if info.get("distance") is not None else None
            if metres is not None:
                distance = "Development is already nearby." if metres <= 100 else f"The nearest development is about {round(metres):,} m away."
        except (TypeError, ValueError):
            pass
        note = "Based on satellite analysis of past growth in the area. It is an outlook, not a guarantee."
        if variant == 0:
            text = _lines(f"📈 Area outlook — {name}", f"Land value potential: {info['label']}", direction, distance, note, "", cta, "", tags)
        elif variant == 1:
            text = _lines("Where is the city growing?", f"Satellite data around {name} shows: {info['label'].lower()} land value potential.", direction, distance, note, "", cta, "", tags)
        else:
            text = _lines(f"Buy where the growth is heading.", direction or f"Outlook for {name}: {info['label']} potential.", distance, note, "", cta, "", tags)

    elif key == "how_to_buy":
        steps = ["1️⃣ Choose your plot on the live map", "2️⃣ Visit the site before you commit", "3️⃣ Reserve it online", "4️⃣ Pay as agreed and receive your documents"]
        heads = [f"How to buy land at {name} — 4 simple steps", "Buying land does not have to be complicated.", f"From choosing to owning: the {name} process"]
        text = _lines(heads[variant], "", *steps, "", cta, "", tags)

    elif key == "why_land":
        if variant == 0:
            text = _lines("Land is a finite asset you can visit, see and plan around.", "Whether you build soon or hold for later, start with the right plot in the right place.", (f"{name} — {where[2:]}" if where else name), "", cta, "", tags)
        elif variant == 1:
            text = _lines("Before you buy land anywhere, always:", "✔ Inspect the site in person", "✔ Verify the documents", "✔ Get a receipt for every payment", "", f"At {name} you can choose your plot on a live map and book a site inspection.", "", cta, "", tags)
        else:
            text = _lines("You don't need to build tomorrow to start owning land today.", f"Secure a plot at {name} now and decide the next step in your own time.", plan, "", cta, "", tags)

    elif key == "faq":
        if variant == 0:
            text = _lines("Can I see the land before I pay?", f"Yes. Book a site inspection at {name} and walk the plot yourself.", "", f"Book or ask us here: {_link(ctx)}", "", tags)
        elif variant == 1 and plan:
            text = _lines("Can I pay in instalments?", f"Yes. At {name} you can pay in stages: {_plan_parts(ctx)}.", "", f"Check the plots: {_link(ctx)}", "", tags)
        else:
            text = _lines("How do I reserve a plot?", "Open the live map, tap the plot you like and reserve it online.", "You will get a receipt for every payment.", "", cta, "", tags)

    elif key == "contact_cta":
        phone = ctx.contact_phone or (f"+{ctx.whatsapp_digits}" if ctx.whatsapp_digits else None)
        if not phone:
            return None
        wa = whatsapp_link(ctx.whatsapp_digits or ctx.contact_phone, f"Hello, I'd like to know more about {name}") if (ctx.whatsapp_digits or ctx.contact_phone) else None
        if variant == 0:
            text = _lines(f"Questions about {name}?", "Our team is ready to help — prices, sizes, payment plan and site visits.", f"📞 {phone}", (f"💬 WhatsApp: {wa}" if wa else None), "", tags)
        elif variant == 1:
            text = _lines(f"Talk to a real person about land at {name}.", f"Call or WhatsApp {phone}.", (wa if wa else None), "", cta, "", tags)
        else:
            text = _lines("Ask us anything before you decide.", f"{name}: {phone}", (f"💬 {wa}" if wa else None), "", tags)

    elif key == "plot_spotlight":
        if plot is None:
            plot = ctx.available_plots[0] if ctx.available_plots else None
        if plot is None:
            return None
        plot_price = naira(plot.asking_price) if (ctx.show_prices and plot.asking_price is not None) else None
        size = area_text(plot.area_sqm) if plot.area_sqm else None
        link = f"View this plot and reserve: {_link(ctx, plot)}"
        if variant == 0:
            text = _lines(f"📌 Plot {plot.plot_number} at {name}", size, plot_price, where, "", link, "", tags)
        elif variant == 1:
            text = _lines(f"Featured plot: {plot.plot_number}", " · ".join(item for item in (size, plot_price) if item) or None, f"{name}", where, "", link, "", tags)
        else:
            text = _lines(f"This could be yours: Plot {plot.plot_number}", size, plot_price, plan, "", link, "", tags)

    if text is None:
        return None
    return _finish(text, ctx, tone)


def build_templates(db: Session, ctx: MarketingContext, plot: EstatePlot | None = None) -> list[dict]:
    """What the composer offers: every applicable template (wording 1, friendly tone) plus a blank."""
    items: list[dict] = []
    keys = applicable_keys(db, ctx) if ctx.available > 0 else ["new_plots", "how_to_buy", "faq", "why_land"]
    if plot is not None and "plot_spotlight" not in keys:
        keys.insert(0, "plot_spotlight")
    for key in keys:
        caption = render(key, db, ctx, variant=0, tone="friendly", plot=plot if key == "plot_spotlight" else None)
        if caption:
            label, strategy, hint = TEMPLATE_INFO[key]
            items.append({"key": key, "label": label, "strategy": strategy, "hint": hint, "caption": caption})
    if plot is not None:  # a chosen plot leads
        items.sort(key=lambda item: 0 if item["key"] == "plot_spotlight" else 1)
    items.append({"key": "custom", "label": "Write my own", "strategy": "Your words", "hint": "Start from a blank caption", "caption": _lines("", "", _link(ctx))})
    return items


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
