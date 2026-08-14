from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine

# Parallel to app.utils.auth_security's Green auth pattern (hashed opaque tokens, SES-style
# session ids, expiry-based sessions) but against its own tables - issue_auth_session /
# resolve_request_session there are hardcoded to green_users / green_sponsor_accounts, so Survey
# gets its own copy of the pattern rather than reusing those functions directly.

_SURVEY_AUTH_SCHEMA_READY = False
_SURVEY_AUTH_SCHEMA_LOCK = Lock()

SESSION_TTL_HOURS = 24 * 30  # long-lived: this is a low-friction consumer product, not an admin console
MAGIC_LINK_TTL_MINUTES = 15


def _utcnow() -> datetime:
    return datetime.utcnow()


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


def _clean_text(value: Any, max_len: int = 255) -> str | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    return text_value[:max_len]


@dataclass(slots=True)
class SurveySessionContext:
    session_uid: str
    user_id: int
    email: str
    full_name: str | None
    expires_at: datetime


def ensure_survey_auth_schema() -> None:
    global _SURVEY_AUTH_SCHEMA_READY
    if _SURVEY_AUTH_SCHEMA_READY:
        return
    with _SURVEY_AUTH_SCHEMA_LOCK:
        if _SURVEY_AUTH_SCHEMA_READY:
            return
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS survey_users (
                        id BIGSERIAL PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        full_name TEXT,
                        google_sub TEXT UNIQUE,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        last_login_at TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS survey_auth_sessions (
                        id BIGSERIAL PRIMARY KEY,
                        session_uid TEXT NOT NULL UNIQUE,
                        access_token_hash TEXT NOT NULL UNIQUE,
                        user_id BIGINT NOT NULL REFERENCES survey_users(id),
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        expires_at TIMESTAMP NOT NULL,
                        revoked_at TIMESTAMP,
                        revoke_reason TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_survey_auth_sessions_user
                    ON survey_auth_sessions (user_id, created_at DESC)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS survey_magic_link_tokens (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL REFERENCES survey_users(id),
                        token_hash TEXT NOT NULL UNIQUE,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        expires_at TIMESTAMP NOT NULL,
                        used_at TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_survey_magic_link_tokens_user
                    ON survey_magic_link_tokens (user_id, created_at DESC)
                    """
                )
            )
        _SURVEY_AUTH_SCHEMA_READY = True


def find_or_create_survey_user(db: Session, *, email: str, full_name: str | None = None) -> int:
    ensure_survey_auth_schema()
    clean_email = str(email or "").strip().lower()
    if not clean_email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    row = db.execute(
        text("SELECT id FROM survey_users WHERE email = :email LIMIT 1"),
        {"email": clean_email},
    ).mappings().first()
    if row:
        return int(row["id"])
    row = db.execute(
        text(
            """
            INSERT INTO survey_users (email, full_name)
            VALUES (:email, :full_name)
            RETURNING id
            """
        ),
        {"email": clean_email, "full_name": _clean_text(full_name, 255)},
    ).mappings().first()
    db.commit()
    return int(row["id"])


def find_or_create_survey_user_by_google(db: Session, *, google_sub: str, email: str, full_name: str | None) -> int:
    ensure_survey_auth_schema()
    clean_sub = str(google_sub or "").strip()
    clean_email = str(email or "").strip().lower()
    if not clean_sub or not clean_email:
        raise HTTPException(status_code=400, detail="Google account did not return an id/email")
    row = db.execute(
        text("SELECT id FROM survey_users WHERE google_sub = :sub OR email = :email LIMIT 1"),
        {"sub": clean_sub, "email": clean_email},
    ).mappings().first()
    if row:
        db.execute(
            text("UPDATE survey_users SET google_sub = :sub WHERE id = :id AND google_sub IS NULL"),
            {"sub": clean_sub, "id": int(row["id"])},
        )
        db.commit()
        return int(row["id"])
    row = db.execute(
        text(
            """
            INSERT INTO survey_users (email, full_name, google_sub)
            VALUES (:email, :full_name, :sub)
            RETURNING id
            """
        ),
        {"email": clean_email, "full_name": _clean_text(full_name, 255), "sub": clean_sub},
    ).mappings().first()
    db.commit()
    return int(row["id"])


def create_magic_link_token(db: Session, *, user_id: int) -> str:
    ensure_survey_auth_schema()
    # Invalidate any prior unused tokens for this user so only the most recently requested link
    # works - avoids a stale earlier email silently remaining valid.
    db.execute(
        text(
            """
            UPDATE survey_magic_link_tokens
            SET used_at = NOW()
            WHERE user_id = :user_id AND used_at IS NULL
            """
        ),
        {"user_id": user_id},
    )
    raw_token = secrets.token_urlsafe(32)
    expires_at = _utcnow() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
    db.execute(
        text(
            """
            INSERT INTO survey_magic_link_tokens (user_id, token_hash, expires_at)
            VALUES (:user_id, :token_hash, :expires_at)
            """
        ),
        {"user_id": user_id, "token_hash": _hash_token(raw_token), "expires_at": expires_at},
    )
    db.commit()
    return raw_token


def consume_magic_link_token(db: Session, *, raw_token: str) -> int:
    ensure_survey_auth_schema()
    clean_token = str(raw_token or "").strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="Missing token")
    row = db.execute(
        text(
            """
            SELECT id, user_id, expires_at, used_at
            FROM survey_magic_link_tokens
            WHERE token_hash = :token_hash
            LIMIT 1
            """
        ),
        {"token_hash": _hash_token(clean_token)},
    ).mappings().first()
    if not row or row["used_at"] is not None or row["expires_at"] <= _utcnow():
        raise HTTPException(status_code=400, detail="This link is invalid or has expired")
    db.execute(
        text("UPDATE survey_magic_link_tokens SET used_at = NOW() WHERE id = :id"),
        {"id": int(row["id"])},
    )
    db.commit()
    return int(row["user_id"])


def issue_survey_session(db: Session, *, user_id: int, request: Request | None = None) -> dict[str, Any]:
    ensure_survey_auth_schema()
    issued_at = _utcnow()
    expires_at = issued_at + timedelta(hours=SESSION_TTL_HOURS)
    access_token = secrets.token_urlsafe(48)
    session_uid = f"SVS-{secrets.token_hex(12).upper()}"
    db.execute(
        text(
            """
            INSERT INTO survey_auth_sessions (session_uid, access_token_hash, user_id, expires_at)
            VALUES (:session_uid, :access_token_hash, :user_id, :expires_at)
            """
        ),
        {
            "session_uid": session_uid,
            "access_token_hash": _hash_token(access_token),
            "user_id": int(user_id),
            "expires_at": expires_at,
        },
    )
    db.execute(
        text("UPDATE survey_users SET last_login_at = NOW() WHERE id = :id"),
        {"id": int(user_id)},
    )
    db.commit()
    user_row = db.execute(
        text("SELECT email, full_name FROM survey_users WHERE id = :id"),
        {"id": int(user_id)},
    ).mappings().first()
    return {
        "access_token": access_token,
        "session_uid": session_uid,
        "expires_at": expires_at.replace(microsecond=0).isoformat() + "Z",
        "user": {
            "id": int(user_id),
            "email": user_row["email"] if user_row else None,
            "full_name": user_row["full_name"] if user_row else None,
        },
    }


def _request_bearer_token(request: Request) -> str | None:
    authorization = _clean_text(request.headers.get("authorization"), 2000)
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return _clean_text(token, 1500)


def resolve_survey_session(db: Session, request: Request, *, touch: bool = True) -> SurveySessionContext | None:
    existing = getattr(request.state, "survey_session", None)
    if isinstance(existing, SurveySessionContext):
        return existing
    access_token = _request_bearer_token(request)
    if not access_token:
        return None
    ensure_survey_auth_schema()
    row = db.execute(
        text(
            """
            SELECT s.session_uid, s.user_id, s.expires_at, s.revoked_at, u.email, u.full_name
            FROM survey_auth_sessions s
            JOIN survey_users u ON u.id = s.user_id
            WHERE s.access_token_hash = :access_token_hash
            LIMIT 1
            """
        ),
        {"access_token_hash": _hash_token(access_token)},
    ).mappings().first()
    if not row:
        return None
    if row["revoked_at"] is not None or row["expires_at"] <= _utcnow():
        return None
    if touch:
        db.execute(
            text("UPDATE survey_auth_sessions SET last_seen_at = NOW() WHERE session_uid = :session_uid"),
            {"session_uid": row["session_uid"]},
        )
        db.commit()
    session = SurveySessionContext(
        session_uid=str(row["session_uid"]),
        user_id=int(row["user_id"]),
        email=str(row["email"]),
        full_name=row["full_name"],
        expires_at=row["expires_at"],
    )
    request.state.survey_session = session
    return session


def require_survey_session(db: Session, request: Request) -> SurveySessionContext:
    session = resolve_survey_session(db, request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def revoke_survey_session(db: Session, request: Request) -> bool:
    session = resolve_survey_session(db, request, touch=False)
    if not session:
        return False
    db.execute(
        text(
            """
            UPDATE survey_auth_sessions
            SET revoked_at = NOW(), revoke_reason = 'user_logout'
            WHERE session_uid = :session_uid AND revoked_at IS NULL
            """
        ),
        {"session_uid": session.session_uid},
    )
    db.commit()
    return True
