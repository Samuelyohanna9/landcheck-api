from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine


def set_survey_user_context(db: Session, user_id: int | None) -> None:
    if engine.dialect.name != "postgresql":
        return
    normalized_user_id = int(user_id) if user_id is not None else None
    # Session.commit() returns its connection to the pool.  db.py reapplies this intent when the
    # Session begins another transaction, while the pool listeners scrub the physical connection
    # so the intent cannot cross into a different Session.
    db.info["survey_user_id"] = normalized_user_id
    db.execute(
        text("SELECT set_config('app.survey_user_id', :user_id, false)"),
        {"user_id": str(normalized_user_id) if normalized_user_id is not None else ""},
    )


def clear_survey_user_context(db: Session) -> None:
    """Explicitly clear Survey context before a request/job session is returned to the pool."""

    if engine.dialect.name != "postgresql":
        db.info.pop("survey_user_id", None)
        return
    try:
        db.rollback()
        db.execute(text("RESET app.survey_user_id"))
        db.info.pop("survey_user_id", None)
        db.commit()
    except Exception:
        db.info.pop("survey_user_id", None)
        db.rollback()
        raise


def verify_survey_row_security(db: Session) -> None:
    """Verify that the migration-installed Survey RLS boundary is present.

    Runtime API and worker roles are deliberately not allowed to create/drop policies.  The
    migration/bootstrap role installs them once, and this read-only check makes a missing policy
    fail closed during startup or staging validation.
    """

    if engine.dialect.name != "postgresql":
        return

    expected_tables = ("plots", "hazard_analysis_jobs", "survey_georeference_sessions")
    rows = db.execute(
        text(
            """
            SELECT c.relname AS table_name,
                   c.relrowsecurity AS rls_enabled,
                   c.relforcerowsecurity AS force_rls
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(:table_names)
            """
        ),
        {
            "table_names": list(expected_tables),
        },
    ).mappings().all()
    by_table = {str(row["table_name"]): row for row in rows}
    missing = [name for name in expected_tables if name not in by_table]
    if missing:
        raise RuntimeError(
            "Required Survey tables are missing: " + ", ".join(missing)
            + ". Run the one-shot migration/bootstrap process before starting the API."
        )

    policy_rows = db.execute(
        text(
            """
            SELECT tablename, policyname
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = ANY(:table_names)
              AND policyname = ANY(:policy_names)
            """
        ),
        {
            "table_names": list(expected_tables),
            "policy_names": [f"lc_{name}_owner_policy" for name in expected_tables],
        },
    ).mappings().all()
    installed = {(str(row["tablename"]), str(row["policyname"])) for row in policy_rows}
    missing_policies = [
        f"{table}:{table_policy}"
        for table in expected_tables
        for table_policy in (f"lc_{table}_owner_policy",)
        if (table, table_policy) not in installed
    ]
    not_enabled = [
        table
        for table in expected_tables
        if not bool(by_table[table]["rls_enabled"])
    ]
    if missing_policies or not_enabled:
        details = []
        if not_enabled:
            details.append("RLS disabled on " + ", ".join(not_enabled))
        if missing_policies:
            details.append("missing policies " + ", ".join(missing_policies))
        raise RuntimeError("Survey RLS verification failed: " + "; ".join(details))


def ensure_survey_row_security(db: Session) -> None:
    """Backward-compatible alias for the read-only verification function."""

    verify_survey_row_security(db)
