from __future__ import annotations

"""A real WhatsApp conversation inbox: inbound messages from customers, and staff replies.

Every message is stored under the single estate it resolves to, never organisation-wide and never
unscoped - a message from a phone number that resolves to no estate's customer or opt-in list is not
stored here at all (it may still be checked for a STOP reply by the webhook itself). That is a
deliberate privacy boundary: the WhatsApp number is shared across every LandCheck Estates company, so
an unresolved sender could be any other company's customer, not this one's."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateAllocation, EstateCustomer
from app.models.estate_social import EstateMarketingOptin, EstateWhatsappMessage
from app.services.estates import social_whatsapp
from app.services.estates.marketing_common import normalize_phone_digits

logger = logging.getLogger(__name__)

REPLY_WINDOW_HOURS = 24
INBOUND_TYPES = {"text", "image", "document", "audio", "video", "sticker", "location"}


def _phone_candidates(digits: str) -> set[str]:
    """The same number can be stored as 234803... or 0803... - compare against both shapes."""
    local = digits[3:] if digits.startswith("234") else digits
    return {digits, local, f"0{local}"}


def resolve_estate_for_phone(db: Session, digits: str) -> tuple[int, int, int | None] | None:
    """Returns (organization_id, estate_id, customer_id) for an inbound message's sender, or None if
    this number isn't known to any estate's customer list or opt-in list."""
    optin = (
        db.query(EstateMarketingOptin)
        .filter(EstateMarketingOptin.phone_digits == digits)
        .order_by(EstateMarketingOptin.consented_at.desc())
        .first()
    )
    if optin is not None:
        return optin.organization_id, optin.estate_id, None
    candidates = _phone_candidates(digits)
    customer = (
        db.query(EstateCustomer)
        .filter(func.regexp_replace(EstateCustomer.phone, "[^0-9]", "", "g").in_(candidates))
        .order_by(EstateCustomer.updated_at.desc())
        .first()
    )
    if customer is None:
        return None
    allocation = (
        db.query(EstateAllocation)
        .filter(EstateAllocation.customer_id == customer.id)
        .order_by(EstateAllocation.created_at.desc())
        .first()
    )
    if allocation is None:
        return None  # a customer record with no estate relationship yet - nothing to attribute this to
    return customer.organization_id, allocation.estate_id, customer.id


def _message_type_and_media(message: dict[str, Any]) -> tuple[str, str | None, str | None, str | None]:
    """Returns (message_type, body, media_id, mime_type)."""
    kind = str(message.get("type") or "text")
    if kind == "text":
        return "text", str((message.get("text") or {}).get("body") or ""), None, None
    if kind in ("image", "document", "audio", "video", "sticker"):
        media = message.get(kind) or {}
        return kind, str(media.get("caption") or "") or None, str(media.get("id") or "") or None, str(media.get("mime_type") or "") or None
    if kind == "location":
        loc = message.get("location") or {}
        return "location", f"{loc.get('latitude')},{loc.get('longitude')} {loc.get('name') or ''}".strip(), None, None
    return "other", None, None, None


def record_inbound(db: Session, message: dict[str, Any]) -> bool:
    """Stores one inbound WhatsApp message if its sender resolves to a known estate. Returns True if
    it was stored. Idempotent on wa_message_id, since Meta can redeliver the same webhook event."""
    from_raw = str(message.get("from") or "")
    digits = normalize_phone_digits(from_raw)
    if not digits:
        return False
    wa_message_id = str(message.get("id") or "") or None
    if wa_message_id and db.query(EstateWhatsappMessage.id).filter(EstateWhatsappMessage.wa_message_id == wa_message_id).first():
        return False
    resolved = resolve_estate_for_phone(db, digits)
    if resolved is None:
        return False
    organization_id, estate_id, customer_id = resolved
    message_type, body, media_id, mime_type = _message_type_and_media(message)
    db.add(EstateWhatsappMessage(
        organization_id=organization_id, estate_id=estate_id, customer_id=customer_id, phone_digits=digits,
        direction="in", message_type=message_type, body=body, media_id=media_id, media_mime_type=mime_type,
        wa_message_id=wa_message_id, status="received",
    ))
    db.flush()
    return True


def can_reply_freely(db: Session, estate_id: int, phone_digits: str) -> bool:
    """True within 24 hours of the customer's last inbound message (Meta's customer service window) -
    outside it, only a pre-approved template can reach them."""
    last_inbound = (
        db.query(func.max(EstateWhatsappMessage.created_at))
        .filter(EstateWhatsappMessage.estate_id == estate_id, EstateWhatsappMessage.phone_digits == phone_digits, EstateWhatsappMessage.direction == "in")
        .scalar()
    )
    if last_inbound is None:
        return False
    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_inbound < timedelta(hours=REPLY_WINDOW_HOURS)


def send_reply(db: Session, *, organization_id: int, estate_id: int, phone_digits: str, customer_id: int | None, body: str, actor_subject_type: str, actor_subject_id: str) -> EstateWhatsappMessage:
    """Sends a free-form reply - only call this once can_reply_freely() is true; the WhatsApp API
    itself will also reject it outside the window, and that failure is recorded the same way."""
    row = EstateWhatsappMessage(
        organization_id=organization_id, estate_id=estate_id, customer_id=customer_id, phone_digits=phone_digits,
        direction="out", message_type="text", body=body, status="sent",
        sent_by_subject_type=actor_subject_type, sent_by_subject_id=str(actor_subject_id),
    )
    try:
        row.wa_message_id = social_whatsapp.send_text(phone_digits, body) or None
    except social_whatsapp.WhatsAppError as exc:
        row.status = "failed"
        db.add(row)
        db.flush()
        raise
    db.add(row)
    db.flush()
    return row


def conversations_for_estate(db: Session, estate_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    """One row per phone number this estate has exchanged messages with, newest activity first."""
    latest_per_phone = (
        db.query(EstateWhatsappMessage.phone_digits, func.max(EstateWhatsappMessage.created_at).label("last_at"))
        .filter(EstateWhatsappMessage.estate_id == estate_id)
        .group_by(EstateWhatsappMessage.phone_digits)
        .order_by(func.max(EstateWhatsappMessage.created_at).desc())
        .limit(limit)
        .all()
    )
    out = []
    for phone, _last_at in latest_per_phone:
        last_message = (
            db.query(EstateWhatsappMessage)
            .filter(EstateWhatsappMessage.estate_id == estate_id, EstateWhatsappMessage.phone_digits == phone)
            .order_by(EstateWhatsappMessage.created_at.desc())
            .first()
        )
        unread = (
            db.query(func.count(EstateWhatsappMessage.id))
            .filter(EstateWhatsappMessage.estate_id == estate_id, EstateWhatsappMessage.phone_digits == phone, EstateWhatsappMessage.direction == "in", EstateWhatsappMessage.read_by_staff_at.is_(None))
            .scalar()
        )
        customer = db.get(EstateCustomer, last_message.customer_id) if last_message and last_message.customer_id else None
        out.append({
            "phone_digits": phone,
            "customer_name": customer.full_name if customer else None,
            "customer_id": customer.id if customer else None,
            "last_message": {
                "direction": last_message.direction, "type": last_message.message_type, "body": last_message.body,
                "created_at": last_message.created_at,
            } if last_message else None,
            "unread_count": int(unread or 0),
            "can_reply_freely": can_reply_freely(db, estate_id, phone),
        })
    return out


def thread(db: Session, estate_id: int, phone_digits: str, *, limit: int = 200) -> list[EstateWhatsappMessage]:
    return (
        db.query(EstateWhatsappMessage)
        .filter(EstateWhatsappMessage.estate_id == estate_id, EstateWhatsappMessage.phone_digits == phone_digits)
        .order_by(EstateWhatsappMessage.created_at.asc())
        .limit(limit)
        .all()
    )


def mark_read(db: Session, estate_id: int, phone_digits: str) -> int:
    rows = (
        db.query(EstateWhatsappMessage)
        .filter(EstateWhatsappMessage.estate_id == estate_id, EstateWhatsappMessage.phone_digits == phone_digits, EstateWhatsappMessage.direction == "in", EstateWhatsappMessage.read_by_staff_at.is_(None))
        .all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        row.read_by_staff_at = now
    db.flush()
    return len(rows)
