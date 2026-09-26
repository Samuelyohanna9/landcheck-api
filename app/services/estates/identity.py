from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.estate_auth import EstateAccount, EstateAuthSession
from app.models.estate_foundation import EstateOrganization


PASSWORD_ITERATIONS = 260_000
SESSION_TTL_HOURS = 24


@dataclass(frozen=True, slots=True)
class EstateSessionContext:
    session_uid: str
    account_id: int
    organization_id: int
    full_name: str
    email: str
    expires_at: datetime


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def hash_password(password: str) -> str:
    raw = str(password or "")
    if len(raw) < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, iterations, salt, digest_hex = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:108] or "estate-company"


def request_token(request: Request) -> str | None:
    headers = getattr(request, "headers", {}) or {}
    scheme, _, token = str(headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token[:1500] if token else None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_expired(value: datetime) -> bool:
    """Compare both PostgreSQL aware and SQLite/legacy naive UTC values safely."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)


def issue_session(db: Session, account: EstateAccount, request: Request | None = None) -> dict[str, str | None]:
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    token = secrets.token_urlsafe(48)
    session_uid = f"EAS-{secrets.token_hex(12).upper()}"
    row = EstateAuthSession(
        session_uid=session_uid,
        access_token_hash=_hash_token(token),
        account_id=account.id,
        organization_id=account.organization_id,
        client_label=str(request.headers.get("X-LC-Client") or "")[:120] if request else None,
        ip_address=str(request.headers.get("x-forwarded-for") or "")[:255] if request else None,
        user_agent=str(request.headers.get("user-agent") or "")[:500] if request else None,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    return {
        "access_token": token,
        "session_uid": session_uid,
        "expires_at": expires_at.replace(microsecond=0).isoformat() + "Z",
    }


def resolve_session(db: Session, request: Request, *, touch: bool = True) -> EstateSessionContext | None:
    cached = getattr(request.state, "estate_auth_session", None)
    if isinstance(cached, EstateSessionContext):
        return cached
    token = request_token(request)
    if not token:
        return None
    row = (
        db.query(EstateAuthSession, EstateAccount, EstateOrganization)
        .join(EstateAccount, EstateAccount.id == EstateAuthSession.account_id)
        .join(EstateOrganization, EstateOrganization.id == EstateAuthSession.organization_id)
        .filter(EstateAuthSession.access_token_hash == _hash_token(token))
        .one_or_none()
    )
    if not row:
        return None
    session, account, organization = row
    now = datetime.utcnow()
    if (
        session.session_state != "active"
        or session.revoked_at is not None
        or _is_expired(session.expires_at)
        or account.status != "active"
        or organization.status != "active"
        or int(account.organization_id) != int(session.organization_id)
    ):
        return None
    # Only refresh last_seen_at when it is stale. Writing it on every request made all concurrent
    # requests from one user queue on the same row lock (each holding a pooled connection while it
    # waited, and the holder kept the lock until its request finished), which exhausted the pool.
    last_seen = session.last_seen_at
    if last_seen is not None and getattr(last_seen, "tzinfo", None) is not None:
        last_seen = last_seen.replace(tzinfo=None)
    if touch and (last_seen is None or (now - last_seen).total_seconds() > 60):
        session.last_seen_at = now
        db.flush()
    result = EstateSessionContext(
        session_uid=session.session_uid,
        account_id=int(account.id),
        organization_id=int(account.organization_id),
        full_name=account.full_name,
        email=account.email,
        expires_at=session.expires_at,
    )
    request.state.estate_auth_session = result
    return result


def revoke_session(db: Session, request: Request) -> bool:
    token = request_token(request)
    if not token:
        return False
    row = db.query(EstateAuthSession).filter(EstateAuthSession.access_token_hash == _hash_token(token)).one_or_none()
    if not row:
        return False
    row.session_state = "revoked"
    row.revoked_at = datetime.utcnow()
    row.revoke_reason = "user_logout"
    db.flush()
    return True
