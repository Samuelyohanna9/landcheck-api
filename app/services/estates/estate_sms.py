from __future__ import annotations

"""Transactional SMS to Estate customers - payment reminders, allocation and milestone updates.
Mirrors estate_email.py's contract exactly: every public call here is best-effort and never raises,
so a customer without a phone number, an unconfigured gateway, or a transient delivery failure never
blocks the underlying business action (recording a payment, allocating a plot, etc still succeeds
either way). Callers don't need their own try/except - just call it after the real database commit.

Configuration (environment):
  TERMII_API_KEY     - from the Termii dashboard: Settings > API Keys
  TERMII_SENDER_ID    - the approved Sender ID (e.g. "LANDCHECK"), registered in the Termii dashboard
                        under Rental > SMS Sender IDs and approved before first use

SMS is sent on Termii's DND (transactional) route, since every message here is tied to a customer's
existing plot/payment relationship rather than being a marketing broadcast.
"""

import logging
import os
import re

import requests

from app.services.estates.estate_email import format_naira

logger = logging.getLogger(__name__)

TIMEOUT = 20
SMS_API_URL = "https://api.ng.termii.com/api/sms/send"


def _api_key() -> str:
    return str(os.getenv("TERMII_API_KEY") or "").strip()


def _sender_id() -> str:
    return str(os.getenv("TERMII_SENDER_ID") or "").strip()


def configured() -> bool:
    return bool(_api_key() and _sender_id())


def _normalize_phone(raw: str) -> str | None:
    """Termii expects international format with no leading '+' (e.g. 2348012345678). Customers are
    stored however staff typed them (080..., +234..., 234...), so normalize the common local shapes."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if digits.startswith("0") and len(digits) == 11:
        return "234" + digits[1:]
    if digits.startswith("234"):
        return digits
    if len(digits) == 10:
        return "234" + digits
    return digits


class SmsError(RuntimeError):
    pass


def _send_sms(*, to_phone: str, message: str) -> str:
    """Returns Termii's message_id - the handle the delivery-status webhook later matches this send
    back to, so the caller can store it on the notification log row."""
    if not configured():
        raise SmsError("SMS is not configured on this server")
    phone = _normalize_phone(to_phone)
    if not phone:
        raise SmsError("No valid phone number to send to")
    response = requests.post(
        SMS_API_URL,
        json={
            "api_key": _api_key(),
            "to": phone,
            "from": _sender_id(),
            "sms": message,
            "type": "plain",
            "channel": "dnd",
        },
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        raise SmsError(f"Termii returned HTTP {response.status_code}: {data or response.text}")
    return str(data.get("message_id") or "")


def _sms_event_copy(
    event: str,
    *,
    org_name: str,
    estate_name: str,
    plot_number: str,
    amount_just_paid=None,
    payment_due_at=None,
) -> str | None:
    """Short (~160 char) SMS version of estate_email._event_copy's templates. Returns None for an
    event with no SMS template - callers treat that as "nothing to send", not a failure."""
    if event == "reserved":
        return f"{org_name}: Plot {plot_number} at {estate_name} is now reserved in your name. We'll notify you before each payment is due."
    if event == "reservation_expiring":
        return f"{org_name}: Your reservation for Plot {plot_number} at {estate_name} expires soon. Please contact us to confirm payment."
    if event == "allocated":
        return f"{org_name}: Congratulations! Plot {plot_number} at {estate_name} is now officially allocated to you."
    if event == "payment_recorded":
        amount = f" of {format_naira(amount_just_paid)}" if amount_just_paid else ""
        return f"{org_name}: Payment{amount} received for Plot {plot_number} at {estate_name}. Thank you."
    if event == "payment_completed":
        return f"{org_name}: Plot {plot_number} at {estate_name} is now FULLY PAID. Congratulations!"
    if event == "payment_reminder":
        due_label = payment_due_at.strftime("%d %b") if payment_due_at else "soon"
        return f"{org_name}: Reminder - your next payment for Plot {plot_number} at {estate_name} is due {due_label}."
    if event == "survey_ready":
        return f"{org_name}: The official survey plan for Plot {plot_number} at {estate_name} is ready."
    if event == "land_developed":
        return f"{org_name}: Development update - Plot {plot_number} at {estate_name} is now marked developed."
    if event == "staked":
        return f"{org_name}: Plot {plot_number} at {estate_name} has been staked - boundary beacons are now on site."
    return None


MANUAL_SMS_MAX_CHARS = 320
GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENDED = set("^{}\\[~]|€")


def sms_segments(text: str) -> tuple[int, bool]:
    """Returns (segment count, is_unicode). A single non-GSM character (like the naira sign) switches
    the whole message to 70-character segments instead of 160 - worth showing staff before sending."""
    is_gsm = all(ch in GSM_BASIC or ch in GSM_EXTENDED for ch in text)
    length = sum(2 if ch in GSM_EXTENDED else 1 for ch in text) if is_gsm else len(text)
    if is_gsm:
        single, multi = 160, 153
    else:
        single, multi = 70, 67
    if length == 0:
        return 0, not is_gsm
    return (1 if length <= single else -(-length // multi)), not is_gsm


def send_manual_sms(*, to_phone: str, message: str) -> str | None:
    """Sends one hand-written SMS. Returns Termii's message id, or None if it wasn't sent."""
    try:
        return _send_sms(to_phone=to_phone, message=message) or None
    except Exception:
        logger.exception("Manual SMS failed (to=%s)", to_phone)
        return None


def notify_customer_sms(
    *,
    to_phone: str | None,
    org_name: str,
    estate_name: str,
    plot_number: str,
    event: str,
    amount_just_paid=None,
    payment_due_at=None,
) -> str | None:
    """Returns Termii's message_id once the SMS was actually sent - None if there was no phone
    number, no template for this event, SMS isn't configured, or delivery failed. Callers store the
    id on the notification log row so the delivery-status webhook can find it again later."""
    to_phone = str(to_phone or "").strip()
    if not to_phone:
        return None
    message = _sms_event_copy(
        event,
        org_name=org_name,
        estate_name=estate_name,
        plot_number=plot_number,
        amount_just_paid=amount_just_paid,
        payment_due_at=payment_due_at,
    )
    if not message:
        return None
    try:
        return _send_sms(to_phone=to_phone, message=message) or None
    except Exception:
        logger.exception("Estate customer SMS notification failed (event=%s, to=%s)", event, to_phone)
        return None
