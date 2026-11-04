"""Real PostgreSQL RLS checks for Estates tenant isolation.

Set ESTATE_RLS_TEST_DATABASE_URL to an isolated, already-migrated (alembic upgrade head)
PostgreSQL database to run these tests - scripts/test-estates-rls.ps1 does both steps. They
intentionally skip in the normal unit-test environment; they must never point at production.

Unlike test_survey_rls_postgres.py (which creates throwaway tables), these tests run against the
real estate_* tables and the real policies installed by
alembic/versions/20261104_0083_estate_rls_policies.py, since the point is to prove the actual
migration-installed policies behave correctly, not a stand-in."""

from __future__ import annotations

import os
import secrets

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.utils.row_security import ESTATE_LOOKUP_SELECT_TABLES, ESTATE_RLS_TABLES, verify_estate_row_security


DATABASE_URL = str(os.getenv("ESTATE_RLS_TEST_DATABASE_URL") or "").strip()
pytestmark = [
    pytest.mark.estate_rls,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ESTATE_RLS_TEST_DATABASE_URL is not configured; production must never be used for this suite",
    ),
]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@pytest.fixture(scope="module")
def admin_engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def restricted_sessionmaker(admin_engine):
    """A disposable, non-owner, non-superuser login role - the same role shape the real
    landcheck_api runtime connection uses, so RLS is actually enforced against it (the migrator
    owns every table and is exempt from RLS regardless of policy)."""
    role = f"lc_estate_rls_probe_{secrets.token_hex(5)}"
    password = secrets.token_urlsafe(18)
    role_ident = _quote_identifier(role)
    with admin_engine.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role_ident} LOGIN PASSWORD :password NOSUPERUSER NOBYPASSRLS"), {"password": password})
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_ident}"))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role_ident}"))
        conn.execute(text(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role_ident}"))
    restricted_url = admin_engine.url.set(username=role, password=password)
    restricted_engine = create_engine(restricted_url, pool_pre_ping=True)
    yield sessionmaker(bind=restricted_engine)
    restricted_engine.dispose()
    with admin_engine.begin() as conn:
        conn.execute(text(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role_ident}"))
        conn.execute(text(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role_ident}"))
        conn.execute(text(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role_ident}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {role_ident}"))


@pytest.fixture()
def two_orgs(admin_engine):
    """Two organizations, one estate each, one customer under org A - inserted as the admin
    (owner) role so RLS doesn't interfere with setup, cleaned up after each test."""
    with admin_engine.begin() as conn:
        org_a = conn.execute(text(
            "INSERT INTO estate_organizations (organization_uid, name, slug, contact_email, status) "
            "VALUES (gen_random_uuid()::text, 'RLS Test Org A', 'lc-rls-test-org-a-' || substr(gen_random_uuid()::text, 1, 8), 'a@example.com', 'active') RETURNING id"
        )).scalar()
        org_b = conn.execute(text(
            "INSERT INTO estate_organizations (organization_uid, name, slug, contact_email, status) "
            "VALUES (gen_random_uuid()::text, 'RLS Test Org B', 'lc-rls-test-org-b-' || substr(gen_random_uuid()::text, 1, 8), 'b@example.com', 'active') RETURNING id"
        )).scalar()
        estate_a = conn.execute(text(
            "INSERT INTO estate_estates (estate_uid, organization_id, name, boundary, status, created_by_subject_type, created_by_subject_id) "
            "VALUES (gen_random_uuid()::text, :org, 'Estate A', ST_SetSRID(ST_GeomFromText('POLYGON((0 0,0 1,1 1,1 0,0 0))'), 4326), 'active', 'test', 'test') RETURNING id"
        ), {"org": org_a}).scalar()
        customer_a = conn.execute(text(
            "INSERT INTO estate_customers (customer_uid, organization_id, full_name, full_name_normalized, phone) "
            "VALUES (gen_random_uuid()::text, :org, 'Buyer A', 'buyer a', '0800000001') RETURNING id"
        ), {"org": org_a}).scalar()
    yield {"org_a": org_a, "org_b": org_b, "estate_a": estate_a, "customer_a": customer_a}
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM estate_organizations WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b})


def _query_as(restricted_sessionmaker, organization_ids: str, bypass: str, two_orgs: dict) -> dict:
    with restricted_sessionmaker() as session:
        session.execute(text("SELECT set_config('app.estate_organization_ids', :v, false)"), {"v": organization_ids})
        session.execute(text("SELECT set_config('app.estate_bypass', :v, false)"), {"v": bypass})
        orgs = set(session.execute(
            text("SELECT id FROM estate_organizations WHERE id IN (:a, :b)"),
            {"a": two_orgs["org_a"], "b": two_orgs["org_b"]},
        ).scalars().all())
        customers = set(session.execute(
            text("SELECT id FROM estate_customers WHERE id = :c"), {"c": two_orgs["customer_a"]},
        ).scalars().all())
        return {"orgs": orgs, "customers": customers}


def test_migration_installed_rls_on_every_table(admin_engine):
    """The real, ultimate check: verify_estate_row_security (called at API startup) passes
    against this database - proves the migration actually enabled RLS and installed the expected
    policy on all 72 ESTATE_RLS_TABLES, not just the handful this file spot-checks below."""
    with Session(admin_engine) as session:
        verify_estate_row_security(session)


def test_unset_context_is_fail_closed(restricted_sessionmaker, two_orgs):
    result = _query_as(restricted_sessionmaker, organization_ids="", bypass="", two_orgs=two_orgs)
    assert result["orgs"] == set()
    assert result["customers"] == set()


def test_scoped_to_org_a_sees_only_org_a(restricted_sessionmaker, two_orgs):
    result = _query_as(restricted_sessionmaker, organization_ids=str(two_orgs["org_a"]), bypass="", two_orgs=two_orgs)
    assert result["orgs"] == {two_orgs["org_a"]}
    assert result["customers"] == {two_orgs["customer_a"]}


def test_scoped_to_org_b_cannot_see_org_a(restricted_sessionmaker, two_orgs):
    result = _query_as(restricted_sessionmaker, organization_ids=str(two_orgs["org_b"]), bypass="", two_orgs=two_orgs)
    assert result["orgs"] == {two_orgs["org_b"]}
    assert result["customers"] == set()


def test_superadmin_bypass_sees_everything(restricted_sessionmaker, two_orgs):
    result = _query_as(restricted_sessionmaker, organization_ids="", bypass="superadmin", two_orgs=two_orgs)
    assert result["orgs"] == {two_orgs["org_a"], two_orgs["org_b"]}
    assert result["customers"] == {two_orgs["customer_a"]}


def test_lookup_bypass_is_narrow_to_carve_out_tables(restricted_sessionmaker, two_orgs):
    """'lookup' must grant estate_customers (a real carve-out table) but NOT estate_organizations
    (not a carve-out table) - the whole point of scoping the bypass per-table."""
    assert "estate_customers" in ESTATE_LOOKUP_SELECT_TABLES
    assert "estate_organizations" not in ESTATE_LOOKUP_SELECT_TABLES
    result = _query_as(restricted_sessionmaker, organization_ids="", bypass="lookup", two_orgs=two_orgs)
    assert result["customers"] == {two_orgs["customer_a"]}
    assert result["orgs"] == set()


def test_backfilled_organization_id_matches_parent_estate(admin_engine, two_orgs):
    """Drift check for the denormalized organization_id on estate_plots (one of the 10 tables
    backfilled from a parent in 20261104_0082) - must always match its estate's own org."""
    with admin_engine.begin() as conn:
        plot_id = conn.execute(text(
            "INSERT INTO estate_plots (plot_uid, estate_id, organization_id, plot_number, plot_number_normalized, "
            "geometry, area_sqm, created_by_subject_type, created_by_subject_id) "
            "VALUES (gen_random_uuid()::text, :estate, :org, 'RLS-TEST-1', 'rls-test-1', "
            "ST_SetSRID(ST_GeomFromText('POLYGON((0 0,0 1,1 1,1 0,0 0))'), 4326), 100, 'test', 'test') RETURNING id"
        ), {"estate": two_orgs["estate_a"], "org": two_orgs["org_a"]}).scalar()
        mismatch = conn.execute(
            text(
                "SELECT count(*) FROM estate_plots p JOIN estate_estates e ON e.id = p.estate_id "
                "WHERE p.id = :plot AND p.organization_id != e.organization_id"
            ),
            {"plot": plot_id},
        ).scalar()
        conn.execute(text("DELETE FROM estate_plots WHERE id = :plot"), {"plot": plot_id})
    assert mismatch == 0
