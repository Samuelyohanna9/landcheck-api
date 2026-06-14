from __future__ import annotations

import json
from threading import Lock
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine

_ACTIVITY_LOG_SCHEMA_READY = False
_ACTIVITY_LOG_SCHEMA_LOCK = Lock()

_SKIP_LOG_PATHS = {
    "/",
    "/health",
    "/favicon.ico",
    "/green/admin/logs",
    "/green/admin/logs/reset",
    "/green/admin/qr-prints",
    "/green/public/logs",
}


def _normalize_text(value: Any, max_len: int = 255) -> str | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    if len(text_value) > max_len:
        return text_value[:max_len]
    return text_value


def ensure_activity_log_table() -> None:
    global _ACTIVITY_LOG_SCHEMA_READY
    if _ACTIVITY_LOG_SCHEMA_READY:
        return
    with _ACTIVITY_LOG_SCHEMA_LOCK:
        if _ACTIVITY_LOG_SCHEMA_READY:
            return
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS green_activity_logs (
                        id SERIAL PRIMARY KEY,
                        source TEXT,
                        event_type TEXT,
                        actor TEXT,
                        message TEXT,
                        details JSONB,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_green_activity_logs_created_at
                    ON green_activity_logs (created_at DESC, id DESC)
                    """
                )
            )
        _ACTIVITY_LOG_SCHEMA_READY = True


def safe_log_activity(
    db: Session,
    source: str,
    event_type: str,
    message: str,
    actor: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    ensure_activity_log_table()
    resolved_actor = resolve_actor_label(actor, details)
    try:
        db.execute(
            text(
                """
                INSERT INTO green_activity_logs (source, event_type, actor, message, details, created_at)
                VALUES (:source, :event_type, :actor, :message, CAST(:details AS JSONB), NOW())
                """
            ),
            {
                "source": _normalize_text(source, 120) or "backend-api",
                "event_type": _normalize_text(event_type, 120) or "activity",
                "actor": resolved_actor,
                "message": _normalize_text(message, 4000) or "Activity recorded",
                "details": json.dumps(details or {}, default=str),
            },
        )
        db.commit()
    except Exception:
        db.rollback()


def _request_client_label(request: Request) -> str | None:
    return _normalize_text(request.headers.get("X-LC-Client"), 120)


def _resolve_actor_from_details(details: Any) -> str | None:
    if not isinstance(details, dict):
        return None
    preferred_keys = (
        "actor_name",
        "user_name",
        "full_name",
        "resolved_reviewer",
        "reviewer_name",
        "created_by",
        "updated_by",
        "submitted_by",
        "requested_by",
        "assigned_by",
        "assignee_name",
        "sponsor_name",
        "actor",
    )
    for key in preferred_keys:
        value = _normalize_text(details.get(key), 255)
        if value:
            return value
    fallback_email = _normalize_text(details.get("email"), 255)
    if fallback_email:
        return fallback_email
    return None


def resolve_actor_label(actor: str | None, details: Any = None) -> str | None:
    direct_actor = _normalize_text(actor, 255)
    if direct_actor:
        return direct_actor
    return _resolve_actor_from_details(details)


def classify_request_source(request: Request) -> str:
    client_label = _request_client_label(request)
    if client_label:
        return client_label
    path = str(request.url.path or "").strip().lower()
    if path.startswith("/plots"):
        return "survey-plan-api"
    if path.startswith("/hazards/flood"):
        return "flood-web"
    if path.startswith("/hazards"):
        return "hazards-api"
    if path.startswith("/feedback"):
        return "feedback-web"
    if path.startswith("/analytics"):
        return "analytics-api"
    if path.startswith("/green"):
        return "green-api"
    if path.startswith("/health"):
        return "health-api"
    return "backend-api"


def request_is_super_admin(request: Request) -> bool:
    auth_mode = str(request.headers.get("X-LC-Auth-Mode") or "").strip().lower()
    role_key = str(request.headers.get("X-LC-Role-Key") or "").strip().lower()
    return auth_mode == "env_admin" or role_key == "super_admin"


def require_super_admin_request(request: Request) -> None:
    if request_is_super_admin(request):
        return
    raise HTTPException(status_code=403, detail="Super Admin access is required for system logs.")


def should_skip_request_logging(request: Request) -> bool:
    path = str(request.url.path or "").strip().lower()
    method = str(request.method or "").strip().upper()
    if method in {"OPTIONS", "HEAD"}:
        return True
    if path in _SKIP_LOG_PATHS:
        return True
    return False


def _request_query_payload(request: Request) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        clean_key = _normalize_text(key, 120)
        if not clean_key:
            continue
        payload[clean_key] = _normalize_text(value, 240) or ""
        if len(payload) >= 20:
            break
    return payload


def _request_ip(request: Request) -> str | None:
    forwarded = _normalize_text(request.headers.get("x-forwarded-for"), 255)
    if forwarded:
        return _normalize_text(forwarded.split(",")[0], 120)
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return _normalize_text(host, 120)


def build_request_log_details(
    request: Request,
    *,
    status_code: int,
    duration_ms: float,
    error_message: str | None = None,
) -> dict[str, Any]:
    path = str(request.url.path or "").strip()
    return {
        "product": classify_request_source(request),
        "client": _request_client_label(request),
        "method": str(request.method or "").upper(),
        "path": path,
        "query": _request_query_payload(request),
        "status_code": int(status_code),
        "duration_ms": round(float(duration_ms), 2),
        "auth_mode": _normalize_text(request.headers.get("X-LC-Auth-Mode"), 80),
        "app_mode": _normalize_text(request.headers.get("X-LC-Session-App-Mode"), 80),
        "role_key": _normalize_text(request.headers.get("X-LC-Role-Key"), 120),
        "user_id": _normalize_text(request.headers.get("X-LC-User-Id"), 80),
        "organization_id": _normalize_text(request.headers.get("X-LC-Organization-Id"), 80),
        "route_path": _normalize_text(request.headers.get("X-LC-App-Route"), 200),
        "ip_address": _request_ip(request),
        "user_agent": _normalize_text(request.headers.get("user-agent"), 500),
        "error": _normalize_text(error_message, 1000),
    }


def log_request_activity(
    request: Request,
    *,
    status_code: int,
    duration_ms: float,
    error_message: str | None = None,
) -> None:
    if should_skip_request_logging(request):
        return
    db = SessionLocal()
    try:
        path = str(request.url.path or "").strip()
        method = str(request.method or "").upper()
        details = build_request_log_details(
            request,
            status_code=status_code,
            duration_ms=duration_ms,
            error_message=error_message,
        )
        safe_log_activity(
            db,
            source=classify_request_source(request),
            event_type="request_completed" if int(status_code) < 400 else "request_failed",
            actor=resolve_actor_label(_normalize_text(request.headers.get("X-LC-User-Name"), 255), details),
            message=f"{method} {path} -> {int(status_code)}",
            details=details,
        )
    finally:
        db.close()
