from __future__ import annotations

"""Email alerts for the Estate sales team and site-inspection visitors: instant new-lead alerts,
follow-up reminders so leads don't go cold, and booking confirmations / reminders.

Every send is best-effort and never raises - a missing address or SMTP hiccup must not block the
public booking or enquiry that triggered it. WhatsApp is deliberately reached through one-tap
links inside the emails (a real WhatsApp Business API needs Meta approval and per-message fees).
"""

import html
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateOrganization,
    EstateOrganizationMember,
    EstatePlot,
    EstatePublicReservationRequest,
)
from app.models.estate_marketing import EstateInspectionBooking, EstateInspectionSlot
from app.services.estates import estate_email
from app.services.estates.marketing_common import agent_member_for, resolve_campaign, web_url, whatsapp_link

logger = logging.getLogger(__name__)

WAT = timezone(timedelta(hours=1))  # Nigeria has no daylight saving.
SALES_ROLES = ("owner", "manager", "sales", "marketer")
LEADERSHIP_ROLES = ("owner", "manager")
FOLLOW_UP_SCHEDULE_HOURS = {"new": [1, 6, 24, 72], "contacted": [72, 168]}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def format_slot_time(value: datetime) -> str:
    local = _aware(value).astimezone(WAT)
    return local.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " ") + " (WAT)"


def _member_emails(db: Session, organization_id: int, roles: tuple[str, ...]) -> list[str]:
    rows = db.query(EstateOrganizationMember).filter(
        EstateOrganizationMember.organization_id == organization_id,
        EstateOrganizationMember.is_active.is_(True),
        EstateOrganizationMember.role_key.in_(roles),
    ).all()
    return [str(row.contact_email).strip().lower() for row in rows if row.contact_email]


def _dedupe(emails: list[str], *, exclude: set[str] | None = None) -> list[str]:
    seen: set[str] = set(exclude or set())
    result = []
    for email in emails:
        clean = str(email or "").strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _send(to_email: str, subject: str, heading: str, message_html: str, buttons: list[tuple[str, str]] | None = None) -> bool:
    button_html = "".join(estate_email._account_button_html(label=label, url=url) for label, url in (buttons or []) if url)
    try:
        estate_email._send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject=subject,
            body_text=estate_email._account_plain_text(heading, message_html, (buttons or [(None, None)])[0][1] if buttons else None),
            body_html=estate_email._account_wrap_html(heading=heading, message_html=message_html, button_html=button_html),
        )
        return True
    except Exception:
        logger.exception("Estate marketing email failed (to=%s, subject=%s)", to_email, subject)
        return False


def _lead_whatsapp(lead_phone: str, lead_name: str, estate_name: str, plot_number: str, sender: str | None) -> str | None:
    intro = f"Hello {lead_name.split(' ')[0]}, this is {sender or 'the sales team'}"
    return whatsapp_link(lead_phone, f"{intro} regarding Plot {plot_number} at {estate_name}. How can I help you?")


# ── New lead ────────────────────────────────────────────────────────────────────────────────
def notify_new_lead(db: Session, *, estate: Estate, plot: EstatePlot, lead: EstatePublicReservationRequest) -> int:
    """Instantly alert the assigned agent (or, with no agent, the wider sales team). The company
    contact address is already notified by the original reservation email, so it's skipped here."""
    organization = db.get(EstateOrganization, estate.organization_id)
    if organization is None:
        return 0
    campaign = resolve_campaign(db, estate.id, lead.source_code)
    member = agent_member_for(db, campaign)
    if member and member.contact_email:
        recipients = [member.contact_email]
    else:
        recipients = _member_emails(db, organization.id, SALES_ROLES)
    recipients = _dedupe(recipients, exclude={str(organization.contact_email or "").strip().lower()})
    if not recipients:
        return 0
    source_label = f" via {campaign.name}" if campaign else ""
    message_html = (
        f"<p><strong>{html.escape(lead.full_name)}</strong> just asked about "
        f"<strong>Plot {html.escape(plot.plot_number)}</strong> at <strong>{html.escape(estate.name)}</strong>{html.escape(source_label)}.</p>"
        f"<p><strong>Phone:</strong> {html.escape(lead.phone)}</p>"
        + (f"<p><strong>Email:</strong> {html.escape(lead.email)}</p>" if lead.email else "")
        + (f"<p><strong>Message:</strong><br/>{html.escape(lead.message)}</p>" if lead.message else "")
        + "<p>Leads that get a reply within the hour are far more likely to become reservations. Tap below to reach out now.</p>"
    )
    buttons = []
    chat = _lead_whatsapp(lead.phone, lead.full_name, estate.name, plot.plot_number, member.subject_id if member else organization.name)
    if chat:
        buttons.append(("Reply on WhatsApp", chat))
    sent = 0
    for email in recipients:
        if _send(email, f"New lead - Plot {plot.plot_number} ({lead.full_name})", "New plot enquiry", message_html, buttons):
            sent += 1
    return sent


# ── Follow-up reminders ─────────────────────────────────────────────────────────────────────
def send_lead_followup_reminders(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """One digest email per recipient listing leads still waiting for a reply. New leads are
    nudged at 1h / 6h / 24h / 72h; contacted-but-unconverted leads at 3 and 7 days. Leads past
    24h are escalated to owners and managers as well as the assigned agent."""
    now = now or datetime.now(timezone.utc)
    horizon = now - timedelta(days=30)
    rows = db.query(EstatePublicReservationRequest).filter(
        EstatePublicReservationRequest.status.in_(("new", "contacted")),
        EstatePublicReservationRequest.created_at >= horizon,
    ).all()

    due: list[tuple[EstatePublicReservationRequest, int]] = []
    for row in rows:
        schedule = FOLLOW_UP_SCHEDULE_HOURS[row.status]
        count = int(row.follow_up_reminder_count or 0)
        if count >= len(schedule):
            continue
        reference = _aware(row.created_at) if row.status == "new" else _aware(row.contacted_at or row.updated_at)
        if reference is None or now - reference < timedelta(hours=schedule[count]):
            continue
        due.append((row, count))

    digests: dict[str, list[dict]] = {}
    org_cache: dict[int, dict] = {}
    for row, count in due:
        info = org_cache.get(row.organization_id)
        if info is None:
            organization = db.get(EstateOrganization, row.organization_id)
            info = {
                "organization": organization,
                "sales": _member_emails(db, row.organization_id, SALES_ROLES),
                "leaders": _member_emails(db, row.organization_id, LEADERSHIP_ROLES),
            }
            org_cache[row.organization_id] = info
        estate = db.get(Estate, row.estate_id)
        plot = db.get(EstatePlot, row.plot_id)
        campaign = resolve_campaign(db, row.estate_id, row.source_code)
        member = agent_member_for(db, campaign)
        recipients: list[str] = []
        if member and member.contact_email:
            recipients.append(member.contact_email)
            if count >= 2:
                recipients += info["leaders"]
        else:
            recipients += info["sales"]
        if count >= 2 and info["organization"] is not None and info["organization"].contact_email:
            recipients.append(info["organization"].contact_email)
        item = {
            "row": row,
            "count": count,
            "estate_name": estate.name if estate else "Estate",
            "plot_number": plot.plot_number if plot else "-",
            "agent": member.subject_id if member else None,
            "age_hours": int((now - (_aware(row.created_at) or now)).total_seconds() // 3600),
        }
        item["recipients"] = _dedupe(recipients)
        for email in item["recipients"]:
            digests.setdefault(email, []).append(item)

    delivered: dict[int, bool] = {}
    emails_sent = 0
    for email, items in digests.items():
        lines = []
        for item in items:
            lead = item["row"]
            chat = _lead_whatsapp(lead.phone, lead.full_name, item["estate_name"], item["plot_number"], item["agent"])
            age = f"{item['age_hours']}h ago" if item["age_hours"] < 48 else f"{item['age_hours'] // 24} days ago"
            status = "still waiting for a reply" if lead.status == "new" else "contacted but not yet reserved"
            lines.append(
                f"<li><strong>{html.escape(lead.full_name)}</strong> - Plot {html.escape(item['plot_number'])}, {html.escape(item['estate_name'])} "
                f"({html.escape(status)}, enquired {html.escape(age)})<br/>"
                f"<a href=\"tel:{html.escape(lead.phone)}\">{html.escape(lead.phone)}</a>"
                + (f" &middot; <a href=\"{html.escape(chat)}\">WhatsApp</a>" if chat else "")
                + "</li>"
            )
        noun = "lead needs" if len(items) == 1 else "leads need"
        message_html = f"<p><strong>{len(items)} {noun} a follow-up.</strong></p><ul>{''.join(lines)}</ul><p>Reply quickly, then mark the lead as contacted in your workspace so reminders stop.</p>"
        ok = _send(email, f"{len(items)} {noun} a follow-up", "Leads waiting for you", message_html)
        emails_sent += 1 if ok else 0
        for item in items:
            delivered[item["row"].id] = delivered.get(item["row"].id, False) or ok

    reminded = 0
    for row, _count in due:
        no_recipients = not any(row.id == item["row"].id for items in digests.values() for item in items)
        if delivered.get(row.id) or no_recipients:
            row.follow_up_reminder_count = int(row.follow_up_reminder_count or 0) + 1
            row.last_follow_up_reminder_at = now
            reminded += 1
    db.flush()
    return {"leads_reminded": reminded, "emails_sent": emails_sent}


# ── Inspections ─────────────────────────────────────────────────────────────────────────────
def meeting_point_link(estate: Estate) -> str | None:
    point = estate.public_meeting_point if isinstance(estate.public_meeting_point, dict) else None
    if point and point.get("lat") is not None and point.get("lng") is not None:
        return f"https://www.google.com/maps/search/?api=1&query={point['lat']},{point['lng']}"
    return None


def calendar_link(estate: Estate, slot: EstateInspectionSlot) -> str:
    start = _aware(slot.starts_at).astimezone(timezone.utc)
    end = start + timedelta(minutes=int(slot.duration_minutes or 120))
    point = estate.public_meeting_point if isinstance(estate.public_meeting_point, dict) else {}
    location = point.get("label") or estate.location_text or estate.name
    fmt = "%Y%m%dT%H%M%SZ"
    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote('Site inspection - ' + estate.name)}"
        f"&dates={start.strftime(fmt)}/{end.strftime(fmt)}"
        f"&location={quote(str(location))}"
        f"&details={quote(str(point.get('note') or slot.note or ''))}"
    )


def manage_url(token: str) -> str:
    return f"{web_url()}/estates/inspection/{token}"


def _booking_details_html(estate: Estate, slot: EstateInspectionSlot, booking: EstateInspectionBooking, plot: EstatePlot | None) -> str:
    point = estate.public_meeting_point if isinstance(estate.public_meeting_point, dict) else {}
    lines = [
        f"<p><strong>{html.escape(estate.name)}</strong><br/>{html.escape(format_slot_time(slot.starts_at))}</p>",
        f"<p><strong>Guests:</strong> {int(booking.party_size)}"
        + (f" &middot; <strong>Interested in:</strong> Plot {html.escape(plot.plot_number)}" if plot else "")
        + "</p>",
    ]
    if point.get("label") or point.get("note"):
        lines.append(f"<p><strong>Meeting point:</strong> {html.escape(str(point.get('label') or ''))}<br/>{html.escape(str(point.get('note') or ''))}</p>")
    if slot.note:
        lines.append(f"<p>{html.escape(slot.note)}</p>")
    return "".join(lines)


def send_booking_confirmation(db: Session, *, estate: Estate, slot: EstateInspectionSlot, booking: EstateInspectionBooking, plot: EstatePlot | None, manage_token: str) -> bool:
    if not booking.email:
        return False
    buttons = [("Add to calendar", calendar_link(estate, slot))]
    map_link = meeting_point_link(estate)
    if map_link:
        buttons.append(("Get directions to the meeting point", map_link))
    buttons.append(("Manage or cancel booking", manage_url(manage_token)))
    message_html = (
        f"<p>Hello {html.escape(booking.full_name)}, your site inspection is booked.</p>"
        + _booking_details_html(estate, slot, booking, plot)
        + "<p>We will send a reminder the day before. If your plans change, please cancel so someone else can take your place.</p>"
    )
    return _send(booking.email, f"Inspection booked - {estate.name}", "Your site inspection is confirmed", message_html, buttons)


def notify_staff_of_booking(db: Session, *, estate: Estate, slot: EstateInspectionSlot, booking: EstateInspectionBooking, plot: EstatePlot | None) -> int:
    organization = db.get(EstateOrganization, estate.organization_id)
    if organization is None:
        return 0
    campaign = resolve_campaign(db, estate.id, booking.source_code)
    member = agent_member_for(db, campaign)
    recipients = [member.contact_email] if (member and member.contact_email) else _member_emails(db, organization.id, SALES_ROLES)
    recipients = _dedupe(recipients + ([organization.contact_email] if organization.contact_email else []))
    message_html = (
        f"<p><strong>{html.escape(booking.full_name)}</strong> booked a place on the site inspection.</p>"
        + _booking_details_html(estate, slot, booking, plot)
        + f"<p><strong>Phone:</strong> {html.escape(booking.phone)}</p>"
        + (f"<p><strong>Note:</strong> {html.escape(booking.note)}</p>" if booking.note else "")
    )
    buttons = []
    chat = whatsapp_link(booking.phone, f"Hello {booking.full_name.split(' ')[0]}, thanks for booking a site inspection at {estate.name}. We look forward to seeing you.")
    if chat:
        buttons.append(("Message on WhatsApp", chat))
    return sum(1 for email in recipients if _send(email, f"New inspection booking - {booking.full_name}", "New inspection booking", message_html, buttons))


def send_slot_cancellation(db: Session, *, estate: Estate, slot: EstateInspectionSlot, bookings: list[EstateInspectionBooking]) -> int:
    sent = 0
    for booking in bookings:
        if not booking.email:
            continue
        message_html = (
            f"<p>Hello {html.escape(booking.full_name)}, we are sorry - the site inspection scheduled for "
            f"<strong>{html.escape(format_slot_time(slot.starts_at))}</strong> at {html.escape(estate.name)} has been cancelled.</p>"
            "<p>Please visit the estate page to choose another inspection time.</p>"
        )
        page = f"{web_url()}/estates/public/{estate.public_slug}#inspections"
        if _send(booking.email, f"Inspection cancelled - {estate.name}", "Your inspection was cancelled", message_html, [("Choose another time", page)]):
            sent += 1
    return sent


def send_inspection_reminders(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    window_end = now + timedelta(hours=25)
    slots = db.query(EstateInspectionSlot).filter(
        EstateInspectionSlot.status == "open",
        EstateInspectionSlot.starts_at > now,
        EstateInspectionSlot.starts_at <= window_end,
    ).all()
    visitor_reminders = 0
    staff_digests = 0
    for slot in slots:
        estate = db.get(Estate, slot.estate_id)
        if estate is None:
            continue
        starts = _aware(slot.starts_at)
        hours_to = (starts - now).total_seconds() / 3600
        bookings = db.query(EstateInspectionBooking).filter(EstateInspectionBooking.slot_id == slot.id, EstateInspectionBooking.status == "booked").all()
        for booking in bookings:
            plot = db.get(EstatePlot, booking.plot_id) if booking.plot_id else None
            due_2h = hours_to <= 2.5 and booking.reminder_2h_sent_at is None
            due_24h = 2.5 < hours_to <= 25 and booking.reminder_24h_sent_at is None
            if not (due_2h or due_24h):
                continue
            label = "in about 2 hours" if due_2h else "tomorrow"
            if booking.email:
                buttons = []
                map_link = meeting_point_link(estate)
                if map_link:
                    buttons.append(("Get directions to the meeting point", map_link))
                message_html = (
                    f"<p>Hello {html.escape(booking.full_name)}, a reminder that your site inspection is {label}.</p>"
                    + _booking_details_html(estate, slot, booking, plot)
                    + "<p>Can no longer make it? Use the cancel link in your confirmation email so someone else can take your place.</p>"
                )
                if _send(booking.email, f"Reminder: inspection {label} - {estate.name}", "Your site inspection is coming up", message_html, buttons):
                    visitor_reminders += 1
            if due_2h:
                booking.reminder_2h_sent_at = now
                booking.reminder_24h_sent_at = booking.reminder_24h_sent_at or now
            else:
                booking.reminder_24h_sent_at = now
        if slot.staff_reminder_sent_at is None and bookings:
            organization = db.get(EstateOrganization, estate.organization_id)
            recipients = _dedupe(_member_emails(db, estate.organization_id, SALES_ROLES + ("field_officer",)) + ([organization.contact_email] if organization and organization.contact_email else []))
            guests = sum(int(b.party_size) for b in bookings)
            rows = "".join(f"<li>{html.escape(b.full_name)} ({int(b.party_size)}) - {html.escape(b.phone)}</li>" for b in bookings)
            message_html = f"<p>The site inspection at <strong>{html.escape(estate.name)}</strong> starts <strong>{html.escape(format_slot_time(slot.starts_at))}</strong>. {len(bookings)} booking(s), {guests} guest(s):</p><ul>{rows}</ul>"
            for email in recipients:
                if _send(email, f"Inspection coming up - {len(bookings)} booking(s)", "Site inspection reminder", message_html):
                    staff_digests += 1
            slot.staff_reminder_sent_at = now
    db.flush()
    return {"visitor_reminders": visitor_reminders, "staff_digests": staff_digests}

