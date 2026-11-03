"""Run legacy runtime schema bootstraps once before Uvicorn forks workers.

Alembic owns versioned schema changes. A small set of legacy Survey/Green tables is still
bootstrapped by application code for backward compatibility. This module runs those bootstraps
as a single, fail-fast process from the API entrypoint rather than from every Uvicorn worker.
"""

from __future__ import annotations

import logging

from app.db import SessionLocal
from app.db_init import init_db
from app.routers.plots import ensure_plots_schema_once
from app.routers.survey_georeference import _ensure_schema as ensure_georeference_schema
from app.utils.hazard_jobs import ensure_hazard_analysis_jobs_table
from app.utils.green_photo_evidence import ensure_photo_evidence_schema
from app.utils.osm_overpass import ensure_osm_overpass_schema
from app.utils.plan_reader import ensure_plan_reader_usage_schema
from app.utils.survey_activity import ensure_survey_activity_table
from app.utils.survey_auth_security import ensure_survey_auth_schema
from app.utils.tree_health_ai import ensure_tree_health_schema
from app.routers import green
from app.utils.activity_logger import ensure_activity_log_table


logger = logging.getLogger("landcheck.schema_init")


def initialize_schema() -> None:
    """Complete all remaining legacy schema initialization before API workers start."""

    init_db()
    # These tables predate Alembic in older LandCheck installations.  They are still created by
    # the one-shot migrator/bootstrap process, but API and worker roles are not allowed to execute
    # this DDL during requests.  The migration service sets LANDCHECK_RUNTIME_SCHEMA_BOOTSTRAP=1.
    ensure_survey_auth_schema()
    db = SessionLocal()
    try:
        ensure_plots_schema_once(db)
        ensure_hazard_analysis_jobs_table(db)
        ensure_georeference_schema(db)
        ensure_photo_evidence_schema(db)
        ensure_osm_overpass_schema(db)
        ensure_plan_reader_usage_schema(db)
        ensure_survey_activity_table(db)
        ensure_tree_health_schema(db)
    finally:
        db.close()
    ensure_activity_log_table()
    green.bootstrap_green_schema()
    logger.info("Legacy API schema initialization completed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    initialize_schema()
