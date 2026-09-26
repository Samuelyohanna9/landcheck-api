from __future__ import annotations

"""Content plans: write a run of varied posts about an estate and schedule them automatically.

A plan is "N posts a day" or "N posts a week" for a number of days. Every slot gets a different persuasion
angle from social_templates (announcement, sizes, payment plan, featured plot, how to buy, progress,
outlook, scarcity, proof, inspection, questions, education, contact), rotating so neighbouring posts differ.
Captions are written again from live data just before each post goes out, so a post scheduled on Monday
never says "22 plots left" on Friday when 18 are left - and nothing is posted once plots have sold out."""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate
from app.models.estate_social import EstateSocialAccount, EstateSocialPlan, EstateSocialPost
from app.services.estates import marketing_render, social_templates
from app.services.estates.social_templates import AUTOMATIC_CHANNELS, CHANNEL_FORMAT, LAGOS, TEMPLATE_INFO, TONES, VARIANTS

logger = logging.getLogger(__name__)

DEFAULT_TIMES = {1: ["18:30"], 2: ["09:00", "19:00"], 3: ["08:30", "13:00", "19:30"]}
# Days of the week (Monday = 0) for "N posts a week", spread out evenly.
WEEK_DAYS = {1: [2], 2: [1, 4], 3: [0, 2, 4], 4: [0, 1, 3, 5], 5: [0, 1, 2, 3, 4], 6: [0, 1, 2, 3, 4, 5], 7: [0, 1, 2, 3, 4, 5, 6]}
MAX_POSTS_PER_BATCH = 60
MAX_DAYS = 30
STALE_AFTER = timedelta(hours=12)
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class PlanError(ValueError):
    pass


def clean_times(times: list[str] | None) -> list[str]:
    result = []
    for value in times or []:
        match = TIME_PATTERN.match(str(value).strip())
        if not match:
            raise PlanError(f"'{value}' is not a valid time. Use HH:MM, for example 09:00.")
        result.append(f"{int(match.group(1)):02d}:{match.group(2)}")
    return sorted(dict.fromkeys(result))


def validate_frequency(per_day: int | None, per_week: int | None) -> None:
    if bool(per_day) == bool(per_week):
        raise PlanError("Choose either posts per day or posts per week.")
    if per_day and not 1 <= per_day <= 3:
        raise PlanError("Post up to 3 times a day.")
    if per_week and not 1 <= per_week <= 7:
        raise PlanError("Post between 1 and 7 times a week.")


def make_slots(start: date, days: int, per_day: int | None, per_week: int | None, times: list[str], *, now: datetime | None = None) -> list[datetime]:
    """Posting moments in UTC, from local (Lagos) dates and times, skipping any that have already passed."""
    now = now or datetime.now(timezone.utc)
    slots: list[datetime] = []
    for offset in range(min(max(days, 1), MAX_DAYS)):
        day = start + timedelta(days=offset)
        if per_day:
            picked = list(times[:per_day])
            for fallback in DEFAULT_TIMES[per_day]:  # fill any gap with sensible defaults
                if len(picked) >= per_day:
                    break
                if fallback not in picked:
                    picked.append(fallback)
            moments = sorted(picked)
        else:
            moments = [times[0] if times else "18:30"] if day.weekday() in WEEK_DAYS[per_week or 1] else []
        for moment in moments:
            hour, minute = (int(part) for part in moment.split(":"))
            local = datetime.combine(day, time(hour, minute), tzinfo=LAGOS)
            when = local.astimezone(timezone.utc)
            if when > now + timedelta(minutes=5):
                slots.append(when)
    return slots


ALL_STYLES = ("promo", "luxury", "heritage", "bold", "blueprint")
ROTATION = ("promo", "heritage", "bold", "luxury", "blueprint")


def _style_for(style: str, index: int) -> str:
    return style if style in ALL_STYLES else ROTATION[index % len(ROTATION)]


def generate_items(db: Session, ctx: marketing_render.MarketingContext, *, tone: str, style: str, slots: list[datetime], cursor: int) -> tuple[list[dict[str, Any]], int]:
    """One post per slot, rotating through the templates that have real data behind them."""
    keys = social_templates.applicable_keys(db, ctx)
    if not keys:
        raise PlanError("There are no available plots to advertise right now.")
    items: list[dict[str, Any]] = []
    plots = list(ctx.available_plots)
    spotlight_uses = cursor // max(len(keys), 1)
    for offset, when in enumerate(slots):
        index = cursor + offset
        key = keys[index % len(keys)]
        variant = (index // len(keys)) % VARIANTS
        plot = None
        if key == "plot_spotlight" and plots:
            plot = plots[(spotlight_uses + variant + offset // max(len(keys), 1)) % len(plots)] if len(plots) > 1 else plots[0]
        caption = social_templates.render(key, db, ctx, variant=variant, tone=tone, plot=plot)
        if caption is None:
            key, plot = "new_plots", None
            caption = social_templates.render("new_plots", db, ctx, variant=variant, tone=tone) or ""
        label, strategy, _hint = TEMPLATE_INFO[key]
        items.append({"scheduled_at": when, "template_key": key, "variant": variant, "label": label, "strategy": strategy, "caption": caption, "image_style": _style_for(style, index), "plot_id": plot.id if plot else None})
    return items, cursor + len(slots)


def default_start(now: datetime | None = None) -> date:
    return ((now or datetime.now(timezone.utc)).astimezone(LAGOS) + timedelta(days=0)).date()


def build_preview(db: Session, ctx: marketing_render.MarketingContext, *, days: int, per_day: int | None, per_week: int | None, times: list[str], tone: str, style: str, start: date | None, cursor: int = 0) -> list[dict[str, Any]]:
    validate_frequency(per_day, per_week)
    if tone not in TONES:
        raise PlanError("Choose friendly, professional or urgent.")
    if style not in ALL_STYLES + ("mixed",):
        raise PlanError("Choose one of the designs, or mixed.")
    slots = make_slots(start or default_start(), days, per_day, per_week, clean_times(times))
    if not slots:
        raise PlanError("No posting times fall in the future for those dates. Pick a later start date.")
    if len(slots) > MAX_POSTS_PER_BATCH:
        raise PlanError(f"That would be {len(slots)} posts. Please keep it to {MAX_POSTS_PER_BATCH} or fewer at a time.")
    items, _next = generate_items(db, ctx, tone=tone, style=style, slots=slots, cursor=cursor)
    return items


def _posts_for(plan: EstateSocialPlan, items: list[dict[str, Any]], *, status: str) -> list[EstateSocialPost]:
    posts = []
    for item in items:
        primary = plan.channels[0] if plan.channels else "facebook"
        posts.append(EstateSocialPost(
            organization_id=plan.organization_id, estate_id=plan.estate_id, plot_id=item["plot_id"], plan_id=plan.id, template_key=item["template_key"],
            variant=item["variant"], auto_caption=True, caption=item["caption"], image_format=CHANNEL_FORMAT.get(primary, "post"), image_style=item["image_style"],
            source_code=plan.source_code, channels=list(plan.channels), status=status, scheduled_at=item["scheduled_at"], results={},
            created_by_subject_type=plan.created_by_subject_type, created_by_subject_id=plan.created_by_subject_id,
        ))
    return posts


def create_plan(db: Session, estate: Estate, ctx: marketing_render.MarketingContext, *, name: str | None, days: int, per_day: int | None, per_week: int | None, times: list[str], channels: list[str], tone: str, style: str, start: date | None, auto_renew: bool, approve: bool, source_code: str | None, subject_type: str, subject_id: str) -> tuple[EstateSocialPlan, list[EstateSocialPost]]:
    items = build_preview(db, ctx, days=days, per_day=per_day, per_week=per_week, times=times, tone=tone, style=style, start=start)
    cadence = f"{per_day} a day" if per_day else f"{per_week} a week"
    plan = EstateSocialPlan(
        organization_id=estate.organization_id, estate_id=estate.id, name=(name or f"{cadence.capitalize()} for {min(days, MAX_DAYS)} days")[:120], status="active",
        per_day=per_day, per_week=per_week, times=clean_times(times), channels=list(channels), style=style, tone=tone, duration_days=min(max(days, 1), MAX_DAYS),
        auto_renew=auto_renew, source_code=source_code, cursor=len(items), start_date=start or default_start(), created_by_subject_type=subject_type, created_by_subject_id=str(subject_id),
    )
    db.add(plan)
    db.flush()
    posts = _posts_for(plan, items, status="scheduled" if approve else "draft")
    db.add_all(posts)
    db.flush()
    return plan, posts


def approve_plan(db: Session, plan: EstateSocialPlan) -> int:
    """Turn a plan's drafts into scheduled posts; any whose time has already passed move to the next free slots."""
    drafts = db.query(EstateSocialPost).filter(EstateSocialPost.plan_id == plan.id, EstateSocialPost.status == "draft").order_by(EstateSocialPost.scheduled_at.asc()).all()
    for post in drafts:
        post.status = "scheduled"
    db.flush()
    reslot_overdue(db, plan)
    return len(drafts)


def reslot_overdue(db: Session, plan: EstateSocialPlan, *, now: datetime | None = None) -> int:
    """Posts whose time has passed (a pause, a plan approved late) are spread over the next free slots."""
    now = now or datetime.now(timezone.utc)
    late = db.query(EstateSocialPost).filter(EstateSocialPost.plan_id == plan.id, EstateSocialPost.status == "scheduled", EstateSocialPost.scheduled_at <= now + timedelta(minutes=5)).order_by(EstateSocialPost.scheduled_at.asc()).all()
    if not late:
        return 0
    upcoming = db.query(EstateSocialPost.scheduled_at).filter(EstateSocialPost.plan_id == plan.id, EstateSocialPost.status == "scheduled", EstateSocialPost.scheduled_at > now + timedelta(minutes=5)).all()
    taken = {row[0] for row in upcoming}
    slots = make_slots(default_start(now), MAX_DAYS, plan.per_day, plan.per_week, list(plan.times or []), now=now)
    free = [slot for slot in slots if slot not in taken]
    moved = 0
    for post, slot in zip(late, free):
        post.scheduled_at = slot
        post.reminder_sent_at = None
        moved += 1
    for post in late[moved:]:  # ran out of room - drop rather than pile up
        post.status = "skipped"
        post.results = {"skipped": {"status": "skipped", "error": "The posting time passed while the plan was paused."}}
    db.flush()
    return moved


def refresh_auto_post(db: Session, post: EstateSocialPost) -> bool:
    """Rewrite an automatic post from live data just before it goes out. Returns False when there is nothing
    honest to post (no plots left), and the caller skips the post."""
    estate = db.get(Estate, post.estate_id)
    if estate is None or not (estate.public_enabled and estate.public_slug):
        return False
    ctx = marketing_render.build_context(db, estate, source=post.source_code)
    keys = social_templates.applicable_keys(db, ctx)
    if not keys:
        return False
    plan = db.get(EstateSocialPlan, post.plan_id) if post.plan_id else None
    tone = plan.tone if plan else "friendly"
    key = post.template_key if post.template_key in keys else next((candidate for candidate in ("new_plots", "how_to_buy", "faq") if candidate in keys), keys[0])
    plot = None
    if key == "plot_spotlight":
        plot = next((item for item in ctx.available_plots if item.id == post.plot_id), None) or (ctx.available_plots[0] if ctx.available_plots else None)
    caption = social_templates.render(key, db, ctx, variant=post.variant, tone=tone, plot=plot)
    if not caption:
        return False
    post.caption = caption
    post.template_key = key
    post.plot_id = plot.id if plot else None
    return True


def _has_active_account(db: Session, organization_id: int, channels: list[str]) -> bool:
    needed = {"facebook" if channel == "facebook" else "instagram" for channel in channels if channel in AUTOMATIC_CHANNELS}
    for provider in needed:
        if not db.query(EstateSocialAccount.id).filter(EstateSocialAccount.organization_id == organization_id, EstateSocialAccount.provider == provider, EstateSocialAccount.status == "active").first():
            return False
    return True


def extend_active_plans(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Runs every minute. Repeating plans get their next batch when fewer than two days of posts are left, and
    plans that have run out (and do not repeat) are marked ended."""
    now = now or datetime.now(timezone.utc)
    extended = ended = 0
    for plan in db.query(EstateSocialPlan).filter(EstateSocialPlan.status == "active").limit(200).all():
        pending = db.query(EstateSocialPost.scheduled_at).filter(EstateSocialPost.plan_id == plan.id, EstateSocialPost.status.in_(("scheduled", "draft", "publishing"))).order_by(EstateSocialPost.scheduled_at.desc()).all()
        last = pending[0][0] if pending else None
        if not plan.auto_renew:
            if not pending:
                plan.status = "ended"
                ended += 1
            continue
        if last is not None and last > now + timedelta(days=2):
            continue
        estate = db.get(Estate, plan.estate_id)
        if estate is None or not (estate.public_enabled and estate.public_slug) or not _has_active_account(db, plan.organization_id, list(plan.channels or [])):
            continue
        try:
            ctx = marketing_render.build_context(db, estate, source=plan.source_code)
            start = max((last.astimezone(LAGOS).date() + timedelta(days=1)) if last else default_start(now), default_start(now))
            slots = make_slots(start, plan.duration_days, plan.per_day, plan.per_week, list(plan.times or []), now=now)
            if not slots:
                continue
            items, cursor = generate_items(db, ctx, tone=plan.tone, style=plan.style, slots=slots[:MAX_POSTS_PER_BATCH], cursor=plan.cursor)
        except PlanError:
            continue  # nothing to advertise right now; try again next minute/day
        except Exception:
            logger.exception("Could not extend social plan %s", plan.id)
            db.rollback()
            continue
        db.add_all(_posts_for(plan, items, status="scheduled"))
        plan.cursor = cursor
        extended += 1
    db.commit()
    return {"extended": extended, "ended": ended}


def rewrite_post(db: Session, post: EstateSocialPost) -> bool:
    """Try the next wording of the same angle, written from live data. False when this post has no template
    (a custom caption) or the data for it is gone."""
    if post.template_key not in TEMPLATE_INFO:
        return False
    estate = db.get(Estate, post.estate_id)
    if estate is None:
        return False
    ctx = marketing_render.build_context(db, estate, source=post.source_code)
    plan = db.get(EstateSocialPlan, post.plan_id) if post.plan_id else None
    plot = None
    if post.template_key == "plot_spotlight":
        plot = next((item for item in ctx.available_plots if item.id == post.plot_id), None) or (ctx.available_plots[0] if ctx.available_plots else None)
    variant = (post.variant + 1) % VARIANTS
    caption = social_templates.render(post.template_key, db, ctx, variant=variant, tone=plan.tone if plan else "friendly", plot=plot)
    if not caption:
        return False
    post.caption = caption
    post.variant = variant
    post.auto_caption = True
    return True
