"""Real PostgreSQL RLS and pool-reuse checks for Survey ownership context.

Set SURVEY_RLS_TEST_DATABASE_URL to an isolated PostgreSQL/PostGIS database to run these tests.
They intentionally skip in the normal unit-test environment; they must never point at production.
"""

from __future__ import annotations

import os
import secrets

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db as database
from app.utils import row_security


DATABASE_URL = str(os.getenv("SURVEY_RLS_TEST_DATABASE_URL") or "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="SURVEY_RLS_TEST_DATABASE_URL is not configured; production must never be used for this suite",
)


TABLES = ("plots", "hazard_analysis_jobs", "survey_georeference_sessions")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@pytest.fixture(scope="module")
def rls_database():
    """Create a disposable non-owner login role and the three RLS-protected fixtures."""

    admin_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    role = f"lc_rls_probe_{secrets.token_hex(5)}"
    password = secrets.token_urlsafe(18)
    role_ident = _quote_identifier(role)

    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE ROLE {role_ident} LOGIN PASSWORD :password NOSUPERUSER NOBYPASSRLS"
                ),
                {"password": password},
            )
            conn.execute(text("CREATE TABLE public.plots (id BIGINT PRIMARY KEY, owner_user_id BIGINT)"))
            conn.execute(
                text(
                    "CREATE TABLE public.hazard_analysis_jobs "
                    "(id TEXT PRIMARY KEY, owner_user_id BIGINT, status TEXT NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE public.survey_georeference_sessions "
                    "(id TEXT PRIMARY KEY, owner_user_id BIGINT, status TEXT NOT NULL)"
                )
            )
            for table in TABLES:
                conn.execute(
                    text(
                        f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"
                    )
                )
            conn.execute(text("INSERT INTO plots (id, owner_user_id) VALUES (1, NULL), (2, 101), (3, 202)"))
            conn.execute(
                text(
                    "INSERT INTO hazard_analysis_jobs (id, owner_user_id, status) "
                    "VALUES ('public-hazard', NULL, 'queued'), ('user-a-hazard', 101, 'queued'), "
                    "('user-b-hazard', 202, 'queued')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO survey_georeference_sessions (id, owner_user_id, status) "
                    "VALUES ('public-georef', NULL, 'draft'), ('user-a-georef', 101, 'draft'), "
                    "('user-b-georef', 202, 'draft')"
                )
            )
            predicate = (
                "owner_user_id IS NULL OR "
                "owner_user_id = NULLIF(current_setting('app.survey_user_id', true), '')::bigint"
            )
            for table in TABLES:
                conn.execute(
                    text(
                        f"CREATE POLICY lc_{table}_owner_policy ON {table} "
                        f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
                    )
                )
                conn.execute(
                    text(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role_ident}"
                    )
                )
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_ident}"))

        probe_url = make_url(DATABASE_URL).set(username=role, password=password)
        probe_engine = create_engine(
            probe_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=5,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
        )
        database.attach_survey_context_cleanup(probe_engine)
        yield probe_engine
    finally:
        try:
            with admin_engine.begin() as conn:
                for table in reversed(TABLES):
                    conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                role_exists = conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                    {"role": role},
                ).scalar()
                if role_exists:
                    conn.execute(text(f"DROP OWNED BY {role_ident}"))
                    conn.execute(text(f"DROP ROLE {role_ident}"))
        except Exception:
            # Cleanup must not obscure the actual test result if an intentionally disposable
            # database was externally terminated while the test was running.
            pass
        finally:
            admin_engine.dispose()
            if "probe_engine" in locals():
                probe_engine.dispose()


def _visible_ids(engine, table: str, context: str | None) -> list[str]:
    with engine.connect() as conn:
        conn.execute(
                text("SELECT set_config('app.survey_user_id', :context, false)"),
            {"context": context or ""},
        )
        return [
            str(row[0])
            for row in conn.execute(text(f"SELECT id FROM public.{table} ORDER BY id")).all()
        ]


def test_rls_allows_public_rows_but_isolates_owned_rows(rls_database):
    expected = {
        "plots": ("1", "2", "3"),
        "hazard_analysis_jobs": ("public-hazard", "user-a-hazard", "user-b-hazard"),
        "survey_georeference_sessions": ("public-georef", "user-a-georef", "user-b-georef"),
    }

    for table in TABLES:
        assert _visible_ids(rls_database, table, None) == [expected[table][0]]
        assert _visible_ids(rls_database, table, "101") == [expected[table][0], expected[table][1]]
        assert _visible_ids(rls_database, table, "202") == [expected[table][0], expected[table][2]]


def test_missing_context_does_not_inherit_after_user_switch_or_database_error(rls_database, monkeypatch):
    monkeypatch.setattr(row_security, "engine", rls_database)
    session_factory = sessionmaker(bind=rls_database, autocommit=False, autoflush=False)

    first = session_factory()
    try:
        row_security.set_survey_user_context(first, 101)
        first.commit()
        assert first.execute(text("SELECT id FROM public.plots ORDER BY id")).scalars().all() == [1, 2]
    finally:
        first.close()

    second = session_factory()
    try:
        assert second.execute(text("SELECT id FROM public.plots ORDER BY id")).scalars().all() == [1]
        row_security.set_survey_user_context(second, 202)
        second.commit()
        assert second.execute(text("SELECT id FROM public.plots ORDER BY id")).scalars().all() == [1, 3]
    finally:
        second.close()

    broken = session_factory()
    try:
        row_security.set_survey_user_context(broken, 101)
        with pytest.raises(Exception):
            broken.execute(text("SELECT * FROM table_that_does_not_exist"))
        broken.rollback()
    finally:
        broken.close()

    after_error = session_factory()
    try:
        assert after_error.execute(text("SELECT id FROM public.plots ORDER BY id")).scalars().all() == [1]
    finally:
        after_error.close()


def test_context_survives_intentional_commit_but_is_scrubbed_on_checkin(rls_database, monkeypatch):
    monkeypatch.setattr(row_security, "engine", rls_database)
    session_factory = sessionmaker(bind=rls_database, autocommit=False, autoflush=False)
    session = session_factory()
    try:
        row_security.set_survey_user_context(session, 101)
        session.commit()
        # Session-scoped context is intentional: Survey handlers can commit more than once.
        assert session.execute(text("SELECT current_setting('app.survey_user_id', true)")).scalar() == "101"
    finally:
        session.close()

    fresh = session_factory()
    assert fresh.execute(text("SELECT current_setting('app.survey_user_id', true)")).scalar() in (None, "")
    fresh.rollback()
    fresh.close()


def test_application_scope_entry_points_are_protected_or_explicit_maintenance():
    plots_source = open("app/routers/plots.py", encoding="utf-8").read()
    hazards_source = open("app/routers/hazards.py", encoding="utf-8").read()
    georef_source = open("app/routers/survey_georeference.py", encoding="utf-8").read()
    hazard_jobs_source = open("app/utils/hazard_jobs.py", encoding="utf-8").read()
    worker_source = open("app/services/survey_export_worker.py", encoding="utf-8").read()

    assert "def get_db(request: Request):" in plots_source
    assert "clear_survey_user_context(db)" in plots_source
    assert "def get_db(request: Request):" in hazards_source
    assert "clear_survey_user_context(db)" in hazards_source
    assert "from app.routers.plots import COORDINATE_SYSTEMS, get_db" in georef_source
    assert "from app.utils.row_security import set_survey_user_context" in worker_source
    assert "plots._run_single_plot_export_job" in worker_source
    assert "db.close()" in worker_source
    assert "submit(\n        worker,\n        job_id,\n        owner_user_id," in hazard_jobs_source
    assert "def _run_hazard_analysis_job(job_id: str, owner_user_id: int | None = None)" in hazards_source
    assert "set_survey_user_context(db, owner_user_id)" in hazards_source
    assert "clear_survey_user_context(db)" in hazards_source

