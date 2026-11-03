"""Read-only startup gate for the runtime API/worker database role."""

from __future__ import annotations

from sqlalchemy import text

from app.db import SessionLocal
from app.utils.row_security import verify_survey_row_security


REQUIRED_TABLES = (
    "plots",
    "plot_meta",
    "plot_feature_overrides",
    "plot_subdivision_batches",
    "plot_subdivision_items",
    "plot_export_jobs",
    "hazard_analysis_jobs",
    "survey_georeference_sessions",
    "survey_users",
    "survey_auth_sessions",
    "survey_magic_link_tokens",
    "green_activity_logs",
)


def verify_runtime_schema() -> None:
    db = SessionLocal()
    try:
        missing = []
        for table_name in REQUIRED_TABLES:
            exists = db.execute(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": table_name},
            ).scalar()
            if exists is None:
                missing.append(table_name)
        if missing:
            raise RuntimeError("Required runtime tables are missing: " + ", ".join(missing))
        verify_survey_row_security(db)
    finally:
        db.close()


if __name__ == "__main__":
    verify_runtime_schema()

