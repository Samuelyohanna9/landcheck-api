from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine

_AUTH_SECURITY_SCHEMA_READY = False
_AUTH_SECURITY_SCHEMA_LOCK = Lock()


def _utcnow() -> datetime:
    return datetime.utcnow()


def _clean_text(value: Any, max_len: int = 255) -> str | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    if len(text_value) > max_len:
        return text_value[:max_len]
    return text_value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def is_production_environment() -> bool:
    candidates = (
        "LANDCHECK_ENV",
        "APP_ENV",
        "ENVIRONMENT",
        "FASTAPI_ENV",
        "PYTHON_ENV",
    )
    for name in candidates:
        value = _clean_text(os.getenv(name), 64)
        if value:
            return value.lower() in {"prod", "production", "live"}
    return False


def auth_session_ttl_hours() -> int:
    return max(_env_int("LANDCHECK_AUTH_SESSION_TTL_HOURS", 24), 1)


def auth_session_idle_minutes() -> int | None:
    # Idle-expiry logout is intentionally disabled. Sessions now rely on explicit logout,
    # revocation, and the hard expiry window only.
    return None


def activity_log_retention_days() -> int:
    return max(_env_int("LANDCHECK_ACTIVITY_LOG_RETENTION_DAYS", 365), 30)


def security_session_retention_days() -> int:
    return max(_env_int("LANDCHECK_SECURITY_SESSION_RETENTION_DAYS", 30), 7)


def allow_activity_log_reset() -> bool:
    if not is_production_environment():
        return True
    return _env_bool("LANDCHECK_ALLOW_LOG_RESET", False)


def require_signed_flutterwave_webhooks() -> bool:
    return is_production_environment() or _env_bool("LANDCHECK_REQUIRE_FLW_WEBHOOK_SIGNATURE", False)


def env_admin_login_enabled() -> bool:
    username = _clean_text(os.getenv("WORK_USERNAME") or os.getenv("VITE_WORK_USERNAME"), 120)
    password = os.getenv("WORK_PASSWORD") or os.getenv("VITE_WORK_PASSWORD")
    return bool(username and password)


def get_env_admin_credentials() -> tuple[str, str] | None:
    username = _clean_text(os.getenv("WORK_USERNAME") or os.getenv("VITE_WORK_USERNAME"), 120)
    password = os.getenv("WORK_PASSWORD") or os.getenv("VITE_WORK_PASSWORD")
    if not username or not password:
        return None
    return username, str(password)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AuthSessionContext:
    session_uid: str
    subject_type: str
    subject_id: int | None
    auth_mode: str
    app_mode: str | None
    role_key: str | None
    organization_id: int | None
    user_id: int | None
    sponsor_account_id: int | None
    display_name: str | None
    session_state: str
    expires_at: datetime
    idle_timeout_at: datetime | None
    metadata: dict[str, Any]
    mfa_enabled: bool = False
    mfa_verified: bool = False

    @property
    def is_super_admin(self) -> bool:
        auth_mode = str(self.auth_mode or "").strip().lower()
        role_key = str(self.role_key or "").strip().lower()
        return auth_mode == "env_admin" or role_key == "super_admin"


def ensure_auth_security_schema() -> None:
    global _AUTH_SECURITY_SCHEMA_READY
    if _AUTH_SECURITY_SCHEMA_READY:
        return
    with _AUTH_SECURITY_SCHEMA_LOCK:
        if _AUTH_SECURITY_SCHEMA_READY:
            return
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS green_auth_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        session_uid TEXT NOT NULL UNIQUE,
                        access_token_hash TEXT NOT NULL UNIQUE,
                        subject_type TEXT NOT NULL,
                        subject_id INTEGER,
                        auth_mode TEXT NOT NULL,
                        app_mode TEXT,
                        role_key TEXT,
                        organization_id INTEGER,
                        user_id INTEGER,
                        sponsor_account_id INTEGER,
                        display_name TEXT,
                        session_state TEXT NOT NULL DEFAULT 'active',
                        client_label TEXT,
                        ip_address TEXT,
                        user_agent TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        expires_at TIMESTAMP NOT NULL,
                        idle_timeout_at TIMESTAMP,
                        revoked_at TIMESTAMP,
                        revoke_reason TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_green_auth_sessions_state_expires
                    ON green_auth_sessions (session_state, expires_at DESC)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_green_auth_sessions_subject
                    ON green_auth_sessions (subject_type, subject_id, created_at DESC)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_users
                    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_users
                    ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_users
                    ADD COLUMN IF NOT EXISTS mfa_secret TEXT
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_users
                    ADD COLUMN IF NOT EXISTS mfa_backup_codes JSONB NOT NULL DEFAULT '[]'::jsonb
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_users
                    ADD COLUMN IF NOT EXISTS mfa_enabled_at TIMESTAMP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_users
                    ADD COLUMN IF NOT EXISTS last_password_change_at TIMESTAMP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_sponsor_accounts
                    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_sponsor_accounts
                    ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_sponsor_accounts
                    ADD COLUMN IF NOT EXISTS mfa_secret TEXT
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_sponsor_accounts
                    ADD COLUMN IF NOT EXISTS mfa_backup_codes JSONB NOT NULL DEFAULT '[]'::jsonb
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_sponsor_accounts
                    ADD COLUMN IF NOT EXISTS mfa_enabled_at TIMESTAMP
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE green_sponsor_accounts
                    ADD COLUMN IF NOT EXISTS last_password_change_at TIMESTAMP
                    """
                )
            )
        _AUTH_SECURITY_SCHEMA_READY = True


def _request_bearer_token(request: Request) -> str | None:
    authorization = _clean_text(request.headers.get("authorization"), 2000)
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            clean_token = _clean_text(token, 1500)
            if clean_token:
                return clean_token
    if str(request.method or "").strip().upper() in {"GET", "HEAD"}:
        query_token = _clean_text(request.query_params.get("access_token"), 1500)
        if query_token:
            return query_token
    return None


def _session_context_from_row(row: dict[str, Any], *, mfa_enabled: bool = False, mfa_verified: bool = False) -> AuthSessionContext:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return AuthSessionContext(
        session_uid=str(row.get("session_uid") or "").strip(),
        subject_type=str(row.get("subject_type") or "").strip(),
        subject_id=int(row.get("subject_id") or 0) or None,
        auth_mode=str(row.get("auth_mode") or "").strip(),
        app_mode=_clean_text(row.get("app_mode"), 120),
        role_key=_clean_text(row.get("role_key"), 120),
        organization_id=int(row.get("organization_id") or 0) or None,
        user_id=int(row.get("user_id") or 0) or None,
        sponsor_account_id=int(row.get("sponsor_account_id") or 0) or None,
        display_name=_clean_text(row.get("display_name"), 255),
        session_state=str(row.get("session_state") or "active").strip(),
        expires_at=row.get("expires_at"),
        idle_timeout_at=row.get("idle_timeout_at"),
        metadata=metadata,
        mfa_enabled=bool(mfa_enabled),
        mfa_verified=bool(mfa_verified),
    )


def issue_auth_session(
    db: Session,
    *,
    subject_type: str,
    subject_id: int | None,
    auth_mode: str,
    app_mode: str | None,
    role_key: str | None = None,
    organization_id: int | None = None,
    user_id: int | None = None,
    sponsor_account_id: int | None = None,
    display_name: str | None = None,
    request: Request | None = None,
    mfa_enabled: bool = False,
    mfa_verified: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_auth_security_schema()
    issued_at = _utcnow()
    expires_at = issued_at + timedelta(hours=auth_session_ttl_hours())
    idle_minutes = auth_session_idle_minutes()
    idle_timeout_at = issued_at + timedelta(minutes=idle_minutes) if idle_minutes else None
    access_token = secrets.token_urlsafe(48)
    session_uid = f"SES-{secrets.token_hex(12).upper()}"
    request_metadata = dict(metadata or {})
    request_metadata.setdefault("issued_at", issued_at.isoformat() + "Z")
    request_metadata.setdefault("mfa_enabled", bool(mfa_enabled))
    request_metadata.setdefault("mfa_verified", bool(mfa_verified))
    db.execute(
        text(
            """
            INSERT INTO green_auth_sessions (
                session_uid,
                access_token_hash,
                subject_type,
                subject_id,
                auth_mode,
                app_mode,
                role_key,
                organization_id,
                user_id,
                sponsor_account_id,
                display_name,
                session_state,
                client_label,
                ip_address,
                user_agent,
                metadata,
                created_at,
                last_seen_at,
                expires_at,
                idle_timeout_at
            )
            VALUES (
                :session_uid,
                :access_token_hash,
                :subject_type,
                :subject_id,
                :auth_mode,
                :app_mode,
                :role_key,
                :organization_id,
                :user_id,
                :sponsor_account_id,
                :display_name,
                'active',
                :client_label,
                :ip_address,
                :user_agent,
                CAST(:metadata AS JSONB),
                NOW(),
                NOW(),
                :expires_at,
                :idle_timeout_at
            )
            """
        ),
        {
            "session_uid": session_uid,
            "access_token_hash": _hash_token(access_token),
            "subject_type": _clean_text(subject_type, 80) or "unknown",
            "subject_id": int(subject_id) if subject_id is not None else None,
            "auth_mode": _clean_text(auth_mode, 80) or "unknown",
            "app_mode": _clean_text(app_mode, 80),
            "role_key": _clean_text(role_key, 120),
            "organization_id": int(organization_id) if organization_id is not None else None,
            "user_id": int(user_id) if user_id is not None else None,
            "sponsor_account_id": int(sponsor_account_id) if sponsor_account_id is not None else None,
            "display_name": _clean_text(display_name, 255),
            "client_label": _clean_text(request.headers.get("X-LC-Client"), 120) if request is not None else None,
            "ip_address": _clean_text(request.headers.get("x-forwarded-for"), 255) if request is not None else None,
            "user_agent": _clean_text(request.headers.get("user-agent"), 500) if request is not None else None,
            "metadata": json.dumps(request_metadata, default=str),
            "expires_at": expires_at,
            "idle_timeout_at": idle_timeout_at,
        },
    )
    if subject_type == "green_user" and user_id is not None:
        db.execute(
            text("UPDATE green_users SET last_login_at = NOW() WHERE id = :user_id"),
            {"user_id": int(user_id)},
        )
    elif subject_type == "sponsor_account" and sponsor_account_id is not None:
        db.execute(
            text("UPDATE green_sponsor_accounts SET last_login_at = NOW() WHERE id = :sponsor_id"),
            {"sponsor_id": int(sponsor_account_id)},
        )
    return {
        "access_token": access_token,
        "session_uid": session_uid,
        "expires_at": expires_at.replace(microsecond=0).isoformat() + "Z",
        "idle_timeout_at": idle_timeout_at.replace(microsecond=0).isoformat() + "Z" if idle_timeout_at else None,
        "mfa_enabled": bool(mfa_enabled),
        "mfa_verified": bool(mfa_verified),
    }


def _expire_session_row(db: Session, *, session_uid: str, reason: str) -> None:
    db.execute(
        text(
            """
            UPDATE green_auth_sessions
            SET session_state = 'expired',
                revoked_at = COALESCE(revoked_at, NOW()),
                revoke_reason = COALESCE(revoke_reason, :reason)
            WHERE session_uid = :session_uid
              AND session_state = 'active'
            """
        ),
        {"session_uid": session_uid, "reason": _clean_text(reason, 255) or "expired"},
    )


def _load_user_session_state(db: Session, user_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT
                u.id,
                COALESCE(u.is_active, TRUE) AS is_active,
                COALESCE(u.allow_green, TRUE) AS allow_green,
                COALESCE(u.allow_work, FALSE) AS allow_work,
                u.organization_id,
                u.full_name,
                u.role,
                u.role_id,
                u.mfa_enabled,
                o.status AS organization_status,
                COALESCE(o.is_active, TRUE) AS organization_is_active,
                r.role_key
            FROM green_users u
            LEFT JOIN green_organizations o ON o.id = u.organization_id
            LEFT JOIN green_roles r ON r.id = u.role_id
            WHERE u.id = :user_id
            LIMIT 1
            """
        ),
        {"user_id": int(user_id)},
    ).mappings().first()
    return dict(row) if row else None


def _load_sponsor_session_state(db: Session, sponsor_account_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT
                id,
                full_name,
                COALESCE(is_active, TRUE) AS is_active,
                COALESCE(mfa_enabled, FALSE) AS mfa_enabled
            FROM green_sponsor_accounts
            WHERE id = :sponsor_id
            LIMIT 1
            """
        ),
        {"sponsor_id": int(sponsor_account_id)},
    ).mappings().first()
    return dict(row) if row else None


def resolve_request_session(db: Session, request: Request, *, touch: bool = True) -> AuthSessionContext | None:
    existing = getattr(request.state, "landcheck_session", None)
    if isinstance(existing, AuthSessionContext):
        return existing
    access_token = _request_bearer_token(request)
    if not access_token:
        return None
    ensure_auth_security_schema()
    row = db.execute(
        text(
            """
            SELECT
                session_uid,
                subject_type,
                subject_id,
                auth_mode,
                app_mode,
                role_key,
                organization_id,
                user_id,
                sponsor_account_id,
                display_name,
                session_state,
                metadata,
                expires_at,
                idle_timeout_at,
                revoked_at
            FROM green_auth_sessions
            WHERE access_token_hash = :access_token_hash
            LIMIT 1
            """
        ),
        {"access_token_hash": _hash_token(access_token)},
    ).mappings().first()
    if not row:
        return None
    data = dict(row)
    metadata = data.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    data["metadata"] = metadata
    now = _utcnow()
    revoked_at = data.get("revoked_at")
    expired = (
        str(data.get("session_state") or "").strip().lower() != "active"
        or revoked_at is not None
        or data.get("expires_at") is None
        or data["expires_at"] <= now
        or (data.get("idle_timeout_at") is not None and data["idle_timeout_at"] <= now)
    )
    if expired:
        _expire_session_row(
            db,
            session_uid=str(data.get("session_uid") or "").strip(),
            reason="Session expired",
        )
        db.commit()
        return None

    mfa_enabled = False
    mfa_verified = False
    subject_type = str(data.get("subject_type") or "").strip().lower()
    if subject_type == "green_user" and int(data.get("user_id") or 0) > 0:
        user_row = _load_user_session_state(db, int(data["user_id"]))
        if not user_row:
            _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="User removed")
            db.commit()
            return None
        if not bool(user_row.get("is_active", True)):
            _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="User inactive")
            db.commit()
            return None
        if user_row.get("organization_id") is not None:
            if not bool(user_row.get("organization_is_active", True)):
                _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="Organization inactive")
                db.commit()
                return None
            if str(user_row.get("organization_status") or "").strip().lower() == "suspended":
                _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="Organization suspended")
                db.commit()
                return None
        mfa_enabled = bool(user_row.get("mfa_enabled", False))
        mfa_verified = not mfa_enabled or bool(metadata.get("mfa_verified"))
        data["display_name"] = _clean_text(user_row.get("full_name"), 255) or data.get("display_name")
        data["role_key"] = _clean_text(user_row.get("role_key") or user_row.get("role"), 120) or data.get("role_key")
        data["organization_id"] = int(user_row.get("organization_id") or 0) or None
    elif subject_type == "sponsor_account" and int(data.get("sponsor_account_id") or 0) > 0:
        sponsor_row = _load_sponsor_session_state(db, int(data["sponsor_account_id"]))
        if not sponsor_row:
            _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="Sponsor removed")
            db.commit()
            return None
        if not bool(sponsor_row.get("is_active", True)):
            _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="Sponsor inactive")
            db.commit()
            return None
        mfa_enabled = bool(sponsor_row.get("mfa_enabled", False))
        mfa_verified = not mfa_enabled or bool(metadata.get("mfa_verified"))
        data["display_name"] = _clean_text(sponsor_row.get("full_name"), 255) or data.get("display_name")
    elif subject_type == "env_admin":
        if not env_admin_login_enabled():
            _expire_session_row(db, session_uid=str(data.get("session_uid") or "").strip(), reason="Environment admin disabled")
            db.commit()
            return None
        mfa_enabled = _env_bool("LANDCHECK_ENV_ADMIN_MFA_ENABLED", False)
        mfa_verified = not mfa_enabled or bool(metadata.get("mfa_verified"))

    if touch:
        next_idle_timeout_at = None
        db.execute(
            text(
                """
                UPDATE green_auth_sessions
                SET last_seen_at = NOW(),
                    idle_timeout_at = :idle_timeout_at
                WHERE session_uid = :session_uid
                """
            ),
            {
                "session_uid": str(data.get("session_uid") or "").strip(),
                "idle_timeout_at": next_idle_timeout_at,
            },
        )
        db.commit()
        data["idle_timeout_at"] = next_idle_timeout_at
    session = _session_context_from_row(data, mfa_enabled=mfa_enabled, mfa_verified=mfa_verified)
    request.state.landcheck_session = session
    return session


def require_authenticated_session(
    db: Session,
    request: Request,
    *,
    auth_modes: set[str] | None = None,
) -> AuthSessionContext:
    session = resolve_request_session(db, request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    if auth_modes:
        normalized = {str(item or "").strip().lower() for item in auth_modes}
        if str(session.auth_mode or "").strip().lower() not in normalized:
            raise HTTPException(status_code=403, detail="You do not have access to this resource")
    return session


def require_super_admin_request(db: Session, request: Request) -> AuthSessionContext:
    session = require_authenticated_session(db, request, auth_modes={"env_admin", "partner_user"})
    if session.is_super_admin:
        return session
    raise HTTPException(status_code=403, detail="Super Admin access is required for this action.")


def revoke_request_session(db: Session, request: Request, *, reason: str = "user_logout") -> bool:
    session = resolve_request_session(db, request, touch=False)
    if not session:
        return False
    db.execute(
        text(
            """
            UPDATE green_auth_sessions
            SET session_state = 'revoked',
                revoked_at = NOW(),
                revoke_reason = :reason
            WHERE session_uid = :session_uid
              AND session_state = 'active'
            """
        ),
        {"session_uid": session.session_uid, "reason": _clean_text(reason, 255) or "user_logout"},
    )
    db.commit()
    return True


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")


def generate_backup_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}" for _ in range(max(count, 1))]


def hash_backup_codes(codes: list[str]) -> list[str]:
    return [_hash_token(str(code or "").strip().upper()) for code in codes if str(code or "").strip()]


def _decode_base32_secret(secret: str) -> bytes:
    clean_secret = str(secret or "").strip().upper().replace(" ", "")
    if not clean_secret:
        raise ValueError("Missing secret")
    padding = "=" * ((8 - (len(clean_secret) % 8)) % 8)
    return base64.b32decode(clean_secret + padding, casefold=True)


def _totp_code_for(secret: str, counter: int) -> str:
    key = _decode_base32_secret(secret)
    message = struct.pack(">Q", int(counter))
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return f"{binary % 1000000:06d}"


def verify_totp_code(secret: str, code: str, *, allowed_drift_steps: int = 1, period_seconds: int = 30) -> bool:
    clean_code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(clean_code) != 6:
        return False
    current_counter = int(time.time() // max(period_seconds, 30))
    for offset in range(-max(allowed_drift_steps, 0), max(allowed_drift_steps, 0) + 1):
        if hmac.compare_digest(_totp_code_for(secret, current_counter + offset), clean_code):
            return True
    return False


def build_totp_uri(secret: str, account_name: str, *, issuer: str = "LandCheck") -> str:
    clean_issuer = quote(str(issuer or "LandCheck").strip())
    clean_account_name = quote(str(account_name or "user").strip())
    clean_secret = str(secret or "").strip().replace(" ", "")
    return (
        f"otpauth://totp/{clean_issuer}:{clean_account_name}"
        f"?secret={clean_secret}&issuer={clean_issuer}&algorithm=SHA1&digits=6&period=30"
    )


def cleanup_security_artifacts(db: Session) -> dict[str, int]:
    ensure_auth_security_schema()
    ensure_cutoff = _utcnow() - timedelta(days=security_session_retention_days())
    log_cutoff = _utcnow() - timedelta(days=activity_log_retention_days())
    sessions_deleted = db.execute(
        text(
            """
            DELETE FROM green_auth_sessions
            WHERE (
                    revoked_at IS NOT NULL
                AND revoked_at < :session_cutoff
            )
               OR (
                    expires_at < :session_cutoff
                AND COALESCE(session_state, '') <> 'active'
            )
            """
        ),
        {"session_cutoff": ensure_cutoff},
    ).rowcount or 0
    logs_deleted = db.execute(
        text(
            """
            DELETE FROM green_activity_logs
            WHERE created_at < :log_cutoff
            """
        ),
        {"log_cutoff": log_cutoff},
    ).rowcount or 0
    db.commit()
    return {
        "deleted_sessions": int(sessions_deleted),
        "deleted_activity_logs": int(logs_deleted),
    }


def get_security_posture(db: Session) -> dict[str, Any]:
    ensure_auth_security_schema()
    active_sessions = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM green_auth_sessions
                WHERE session_state = 'active'
                  AND revoked_at IS NULL
                  AND expires_at > NOW()
                  AND (idle_timeout_at IS NULL OR idle_timeout_at > NOW())
                """
            )
        ).scalar()
        or 0
    )
    expired_sessions = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM green_auth_sessions
                WHERE expires_at <= NOW()
                   OR (idle_timeout_at IS NOT NULL AND idle_timeout_at <= NOW())
                """
            )
        ).scalar()
        or 0
    )
    revoked_sessions = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM green_auth_sessions
                WHERE revoked_at IS NOT NULL
                   OR session_state = 'revoked'
                """
            )
        ).scalar()
        or 0
    )
    mfa_users = int(
        db.execute(text("SELECT COUNT(*) FROM green_users WHERE COALESCE(mfa_enabled, FALSE) = TRUE")).scalar()
        or 0
    )
    mfa_sponsors = int(
        db.execute(
            text("SELECT COUNT(*) FROM green_sponsor_accounts WHERE COALESCE(mfa_enabled, FALSE) = TRUE")
        ).scalar()
        or 0
    )
    return {
        "environment": "production" if is_production_environment() else "non_production",
        "active_sessions": active_sessions,
        "expired_sessions": expired_sessions,
        "revoked_sessions": revoked_sessions,
        "mfa_enabled_users": mfa_users,
        "mfa_enabled_sponsors": mfa_sponsors,
        "auth_session_ttl_hours": auth_session_ttl_hours(),
        "auth_session_idle_minutes": auth_session_idle_minutes(),
        "activity_log_retention_days": activity_log_retention_days(),
        "session_retention_days": security_session_retention_days(),
        "env_admin_login_enabled": env_admin_login_enabled(),
        "allow_activity_log_reset": allow_activity_log_reset(),
        "require_signed_flutterwave_webhooks": require_signed_flutterwave_webhooks(),
    }
