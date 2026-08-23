"""Persistent audit trail for the Survey Plan workflows.

The audit is deliberately server-side: browser events alone cannot prove that an export
was generated or a file was actually served.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.utils.survey_auth_security import resolve_survey_session

logger = logging.getLogger(__name__)


def ensure_survey_activity_table(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS survey_activity_events (
            id BIGSERIAL PRIMARY KEY,
            event_type VARCHAR(120) NOT NULL,
            workflow VARCHAR(40) NOT NULL,
            actor_user_id INTEGER REFERENCES survey_users(id) ON DELETE SET NULL,
            plot_id INTEGER REFERENCES plots(id) ON DELETE SET NULL,
            subdivision_batch_id INTEGER,
            georeference_session_id VARCHAR(64),
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_survey_activity_events_created_at
        ON survey_activity_events (created_at DESC, id DESC)
    """))
    db.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_survey_activity_events_actor
        ON survey_activity_events (actor_user_id, created_at DESC)
    """))
    db.commit()


def log_survey_activity(
    db: Session,
    *,
    event_type: str,
    workflow: str,
    request: Request | None = None,
    plot_id: int | None = None,
    subdivision_batch_id: int | None = None,
    georeference_session_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Best-effort activity write that must never prevent the user action succeeding."""
    try:
        ensure_survey_activity_table(db)
        actor_user_id = None
        if request is not None:
            session = resolve_survey_session(db, request)
            actor_user_id = session.user_id if session else None
        db.execute(
            text("""
                INSERT INTO survey_activity_events (
                    event_type, workflow, actor_user_id, plot_id, subdivision_batch_id,
                    georeference_session_id, details
                ) VALUES (
                    :event_type, :workflow, :actor_user_id, :plot_id, :subdivision_batch_id,
                    :georeference_session_id, CAST(:details AS JSONB)
                )
            """),
            {
                "event_type": str(event_type)[:120],
                "workflow": str(workflow)[:40],
                "actor_user_id": actor_user_id,
                "plot_id": plot_id,
                "subdivision_batch_id": subdivision_batch_id,
                "georeference_session_id": georeference_session_id,
                "details": json.dumps(details or {}, default=str),
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Unable to write Survey activity event %s: %s", event_type, exc)
