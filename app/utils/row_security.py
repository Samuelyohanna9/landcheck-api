from __future__ import annotations

from threading import Lock

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine


_LOCK = Lock()
_POLICY_TABLES: set[str] = set()


def set_survey_user_context(db: Session, user_id: int | None) -> None:
    if engine.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT set_config('app.survey_user_id', :user_id, false)"),
        {"user_id": str(int(user_id)) if user_id is not None else ""},
    )


def ensure_survey_row_security(db: Session) -> None:
    """Enable defense-in-depth RLS for tables that carry Survey ownership.

    The API still performs explicit 404 ownership checks. These policies protect the same rows if
    a future endpoint forgets one, and they become authoritative when production uses a database
    role that is not the table owner. Anonymous rows remain claimable by design until ownership is
    attached after sign-in.
    """
    if engine.dialect.name != "postgresql":
        return
    definitions = {
        "plots": "owner_user_id IS NULL OR owner_user_id = NULLIF(current_setting('app.survey_user_id', true), '')::bigint",
        "hazard_analysis_jobs": "owner_user_id IS NULL OR owner_user_id = NULLIF(current_setting('app.survey_user_id', true), '')::bigint",
        "survey_georeference_sessions": "owner_user_id IS NULL OR owner_user_id = NULLIF(current_setting('app.survey_user_id', true), '')::bigint",
    }
    with _LOCK:
        for table_name, predicate in definitions.items():
            if table_name in _POLICY_TABLES:
                continue
            exists = db.execute(text("SELECT to_regclass(:table_name)"), {"table_name": table_name}).scalar()
            if not exists:
                continue
            policy_name = f"lc_{table_name}_owner_policy"
            db.execute(text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
            db.execute(text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}"))
            db.execute(
                text(
                    f"CREATE POLICY {policy_name} ON {table_name} "
                    f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
            _POLICY_TABLES.add(table_name)
        db.commit()
