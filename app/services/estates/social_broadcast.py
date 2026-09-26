from __future__ import annotations

"""Opt-in handling and WhatsApp template broadcasts to interested buyers."""

import hashlib
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate
from app.models.estate_social import EstateMarketingOptin, EstateWhatsappSend
from app.services.estates import social_whatsapp
from app.services.estates.marketing_common import normalize_phone_digits, share_page_url
from app.services.estates.marketing_render import naira_short

logger = logging.getLogger(__name__)

CONSENT_TEXT = "I agree to receive WhatsApp updates about {estate} (new plots, prices and site visits) from {company}. I can reply STOP at any time."
SEND_BATCH_PER_RUN = 80
SEND_PAUSE_SECONDS = 0.15


def consent_text(estate_name: str, company_name: str) -> str:
    return CONSENT_TEXT.format(estate=estate_name, company=company_name)


def record_optin(db: Session, *, estate: Estate, company_name: str, full_name: str | None, phone: str, source_code: str | None) -> EstateMarketingOptin:
    digits = normalize_phone_digits(phone)
    if not digits:
        raise ValueError("Enter a valid phone number")
    row = db.query(EstateMarketingOptin).filter(EstateMarketingOptin.estate_id == estate.id, EstateMarketingOptin.phone_digits == digits, EstateMarketingOptin.channel == "whatsapp").one_or_none()
    text_shown = consent_text(estate.name, company_name)
    now = datetime.now(timezone.utc)
    if row is None:
        row = EstateMarketingOptin(
            organization_id=estate.organization_id, estate_id=estate.id, full_name=(full_name or "").strip()[:255] or None, phone_digits=digits, channel="whatsapp",
            consent_text=text_shown, source_code=(source_code or "")[:120] or None, status="active",
            unsubscribe_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(), consented_at=now,
        )
        db.add(row)
    else:  # signing up again after opting out is a fresh, explicit consent
        row.status = "active"
        row.revoked_at = None
        row.consent_text = text_shown
        row.consented_at = now
        if full_name:
            row.full_name = full_name.strip()[:255]
    db.flush()
    return row


def revoke_phone_everywhere(db: Session, phone_digits: str) -> int:
    """A STOP reply ends messages from every estate/company that had this number."""
    rows = db.query(EstateMarketingOptin).filter(EstateMarketingOptin.phone_digits == phone_digits, EstateMarketingOptin.status == "active").all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = "revoked"
        row.revoked_at = now
    db.flush()
    return len(rows)


def _params_for(preset: str, *, first_name: str, estate: Estate, detail: str, link: str) -> list[str]:
    return [first_name or "there", estate.name, detail, link]


def queue_broadcast(db: Session, *, estate: Estate, preset: str, detail: str | None, min_price, sent_by: str | None) -> tuple[str, int]:
    """Creates one queued send per active opt-in. The scheduler delivers them in small batches, so a large
    audience never ties up a request and stays within WhatsApp's rate limits."""
    if preset not in social_whatsapp.PRESETS:
        raise ValueError("Unknown message type")
    name = social_whatsapp.template_name(preset)
    optins = db.query(EstateMarketingOptin).filter(EstateMarketingOptin.estate_id == estate.id, EstateMarketingOptin.status == "active", EstateMarketingOptin.channel == "whatsapp").limit(social_whatsapp.MAX_BROADCAST).all()
    default_detail = f"from {naira_short(min_price)}" if min_price else "see the latest details"
    link = share_page_url(estate, source="whatsapp-updates")
    batch = str(uuid.uuid4())
    for optin in optins:
        first = (optin.full_name or "").strip().split(" ")[0] if optin.full_name else ""
        db.add(EstateWhatsappSend(
            organization_id=estate.organization_id, estate_id=estate.id, optin_id=optin.id, batch_uid=batch, template_name=name,
            params=_params_for(preset, first_name=first, estate=estate, detail=(detail or default_detail), link=link), status="queued", sent_by_subject_id=sent_by,
        ))
    db.flush()
    return batch, len(optins)


def process_queued_whatsapp_sends(db: Session) -> dict[str, int]:
    """Deliver queued sends (called every minute). Skips anyone who opted out after the broadcast was queued."""
    if not social_whatsapp.configured():
        return {"sent": 0, "failed": 0}
    rows = db.query(EstateWhatsappSend).filter(EstateWhatsappSend.status == "queued").order_by(EstateWhatsappSend.id.asc()).limit(SEND_BATCH_PER_RUN).with_for_update(skip_locked=True).all()
    sent = failed = 0
    for row in rows:
        optin = db.get(EstateMarketingOptin, row.optin_id) if row.optin_id else None
        if optin is None or optin.status != "active":
            row.status = "skipped"
            row.error = "Contact opted out"
            continue
        try:
            row.provider_message_id = social_whatsapp.send_template(optin.phone_digits, row.template_name, list(row.params or []))
            row.status = "sent"
            sent += 1
        except social_whatsapp.WhatsAppError as exc:
            row.status = "failed"
            row.error = str(exc)[:500]
            failed += 1
        except Exception as exc:  # network trouble: leave a failure note, do not lose the rest of the batch
            logger.exception("WhatsApp send failed")
            row.status = "failed"
            row.error = f"Could not reach WhatsApp: {exc}"[:500]
            failed += 1
        db.commit()
        time.sleep(SEND_PAUSE_SECONDS)
    db.commit()
    return {"sent": sent, "failed": failed}
