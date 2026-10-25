from __future__ import annotations

import logging
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.commercial import EstateDemoRequest
from app.schemas.commercial import EstateDemoRequestCreate
from app.services.estates import estate_email


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/commercial", tags=["commercial"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client_key(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", None) or "unknown")


def _client_fingerprint(request: Request) -> str:
    client_key = _client_key(request).encode("utf-8")
    secret = str(
        os.getenv("LANDCHECK_RATE_LIMIT_SECRET")
        or os.getenv("LANDCHECK_API_PUBLIC_URL")
        or "landcheck-demo-rate-limit"
    ).strip().encode("utf-8")
    return hmac.new(secret, client_key, hashlib.sha256).hexdigest()


def _enforce_demo_rate_limit(db: Session, fingerprint: str) -> None:
    """Rate-limit the same client across workers without Redis.

    The advisory lock serializes the count-and-insert transaction on PostgreSQL. SQLite remains
    suitable for focused tests; its single connection also serializes writes in the test setup.
    """
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") == "postgresql":
        lock_key = int.from_bytes(bytes.fromhex(fingerprint[:16]), byteorder="big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = (
        db.query(EstateDemoRequest.id)
        .filter(
            EstateDemoRequest.client_fingerprint == fingerprint,
            EstateDemoRequest.created_at >= cutoff,
        )
        .count()
    )
    if recent_count >= 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": "3600"},
        )


def _notify_saved_request(db: Session, row: EstateDemoRequest) -> bool:
    notification_sent = False
    try:
        notification_sent = estate_email.send_estate_demo_request_notification(
            request_id=row.id,
            company_name=row.company_name,
            contact_name=row.contact_name,
            email=row.email,
            phone=row.phone,
            message=row.message,
            source_path=row.source_path,
        )
    except Exception:
        logger.exception("Estate demo request notification failed (request_id=%s)", row.id)

    row.notification_status = "sent" if notification_sent else "failed"
    row.notified_at = datetime.now(timezone.utc) if notification_sent else None
    db.commit()
    return notification_sent


def _existing_request_response(db: Session, existing: EstateDemoRequest | None) -> dict | None:
    if existing is None:
        return None
    if existing.notification_status == "failed":
        existing.notification_status = "pending"
        existing.notified_at = None
        db.commit()
        notification_sent = _notify_saved_request(db, existing)
        return {
            "status": "received",
            "request_id": existing.id,
            "already_processed": True,
            "notification_sent": notification_sent,
        }
    return {
        "status": "received",
        "request_id": existing.id,
        "already_processed": True,
    }


@router.post("/estate-demo", status_code=status.HTTP_201_CREATED)
def submit_estate_demo_request(
    request: Request,
    payload: EstateDemoRequestCreate = Body(...),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    db: Session = Depends(get_db),
):
    """Persist a public Estates enquiry and notify LandCheck's business inbox.

    The honeypot response is deliberately indistinguishable from a successful submission. The
    enquiry is never returned through a public read endpoint; internal access requires database
    access or a future authenticated admin workflow.
    """
    if payload.website:
        return {
            "status": "received",
            "request_id": None,
            "already_processed": False,
        }

    key = str(idempotency_key or "").strip() or str(uuid.uuid4())
    if len(key) > 128:
        raise HTTPException(status_code=422, detail="Invalid request key")

    existing = (
        db.query(EstateDemoRequest)
        .filter(EstateDemoRequest.idempotency_key == key)
        .with_for_update()
        .one_or_none()
    )
    existing_response = _existing_request_response(db, existing)
    if existing_response is not None:
        return existing_response

    fingerprint = _client_fingerprint(request)
    _enforce_demo_rate_limit(db, fingerprint)

    existing = (
        db.query(EstateDemoRequest)
        .filter(EstateDemoRequest.idempotency_key == key)
        .with_for_update()
        .one_or_none()
    )
    existing_response = _existing_request_response(db, existing)
    if existing_response is not None:
        return existing_response

    row = EstateDemoRequest(
        idempotency_key=key,
        client_fingerprint=fingerprint,
        company_name=payload.company_name,
        contact_name=payload.contact_name,
        email=payload.email,
        phone=payload.phone,
        message=payload.message,
        source_path=payload.source_path or "/estates/",
        status="new",
        notification_status="pending",
    )
    db.add(row)
    try:
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(EstateDemoRequest).filter(EstateDemoRequest.idempotency_key == key).one_or_none()
        if existing:
            return {
                "status": "received",
                "request_id": existing.id,
                "already_processed": True,
            }
        raise HTTPException(status_code=409, detail="This enquiry was already submitted")

    notification_sent = _notify_saved_request(db, row)

    return {
        "status": "received",
        "request_id": row.id,
        "already_processed": False,
        "notification_sent": notification_sent,
    }
