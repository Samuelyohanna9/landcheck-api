from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.estate_auth import EstateAccount, EstateAuthSession
from app.models.estate_foundation import EstateOrganization
from app.utils.http_security import ESTATE_SESSION_COOKIE, cookie_token


PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_DKLEN = 32
PASSWORD_MAXMEM = 64 * 1024 * 1024
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
    digest = hashlib.scrypt(
        raw.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=PASSWORD_DKLEN,
        maxmem=PASSWORD_MAXMEM,
    )
    return f"scrypt${PASSWORD_SCRYPT_N}${PASSWORD_SCRYPT_R}${PASSWORD_SCRYPT_P}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        parts = str(encoded).split("$")
        if parts[0] == "scrypt" and len(parts) == 6:
            _, n, r, p, salt, digest_hex = parts
            derived = hashlib.scrypt(
                str(password or "").encode("utf-8"),
                salt=salt.encode("utf-8"),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=PASSWORD_DKLEN,
                maxmem=PASSWORD_MAXMEM,
            )
            return hmac.compare_digest(derived.hex(), digest_hex)
        if parts[0] != "pbkdf2_sha256" or len(parts) != 4:
            return False
        _, iterations, salt, digest_hex = parts
        derived = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


_TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def generate_temp_password(length: int = 12) -> str:
    """A random one-time password for a newly invited staff account, sent by email and forced to
    be changed on first login. Excludes visually ambiguous characters (0/O, 1/l/I) since it's
    typically hand-typed once off a phone screen."""
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:108] or "estate-company"


def request_token(request: Request) -> str | None:
    headers = getattr(request, "headers", {}) or {}
    scheme, _, token = str(headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer":
        token = token.strip()
        if token:
            return token[:1500]
    return cookie_token(request, ESTATE_SESSION_COOKIE)


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
    # Only refresh last_seen_at when it is stale, and never wait for it: the row lock this takes is
    # held until the request ends, so concurrent requests from one user used to queue behind each
    # other (each holding a pooled connection). SKIP LOCKED makes everyone but the first skip it.
    if touch:
        db.execute(
            text(
                "UPDATE estate_auth_sessions SET last_seen_at = :now WHERE id IN ("
                "SELECT id FROM estate_auth_sessions WHERE id = :id "
                "AND (last_seen_at IS NULL OR last_seen_at < :cutoff) FOR UPDATE SKIP LOCKED)"
            ),
            {"now": now, "id": session.id, "cutoff": now - timedelta(seconds=60)},
        )
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
