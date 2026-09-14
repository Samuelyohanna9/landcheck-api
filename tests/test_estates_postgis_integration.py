"""Opt-in verification for the real Estate schema; never falls back to SQLite."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


URL = str(os.getenv("LANDCHECK_POSTGIS_TEST_DATABASE_URL") or "").strip()
pytestmark = pytest.mark.postgis


def _engine():
    if not URL:
        pytest.skip("LANDCHECK_POSTGIS_TEST_DATABASE_URL is not configured")
    if not URL.startswith(("postgresql://", "postgresql+")) or "test" not in URL.lower():
        raise RuntimeError("LANDCHECK_POSTGIS_TEST_DATABASE_URL must be a dedicated PostgreSQL test database")
    return create_engine(URL, future=True)


def test_estate_postgis_schema_is_migrated():
    engine = _engine()
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT PostGIS_Version()")).scalar()
            tables = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'estate_%'"))).copy()
            assert {"estate_estates", "estate_blocks", "estate_plots", "estate_allocations", "estate_payments", "estate_documents", "estate_document_links", "estate_hazard_assessments"} <= {row[0] for row in tables}
            geometry_types = conn.execute(text("SELECT f_table_name FROM geometry_columns WHERE f_table_name IN ('estate_estates','estate_blocks','estate_plots')")).scalars().all()
            assert set(geometry_types) == {"estate_estates", "estate_blocks", "estate_plots"}
            indexes = set(conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")).scalars())
            assert {"idx_estates_boundary_gist", "idx_blocks_geometry_gist", "idx_plots_geometry_gist", "uq_active_estate_allocation_plot", "uq_estate_payment_reference"} <= indexes
            assert conn.execute(text("SELECT 1 FROM pg_trigger WHERE tgname='estate_audit_events_no_update_delete'")).scalar() == 1
    finally:
        engine.dispose()


def test_estate_database_constraints_and_audit_trigger():
    engine = _engine()
    try:
        with engine.begin() as conn:
            token = uuid4().hex
            org_id = conn.execute(text("INSERT INTO estate_organizations (organization_uid,name,slug) VALUES (:uid,'PostGIS A',:slug) RETURNING id"), {"uid": token, "slug": f"postgis-{token}"}).scalar_one()
            estate_id = conn.execute(text("INSERT INTO estate_estates (estate_uid,organization_id,name,created_by_subject_type,created_by_subject_id,boundary) VALUES (:uid,:org,'Integration Estate','test','owner',ST_GeomFromText('POLYGON((3 6,3.01 6,3.01 6.01,3 6))',4326)) RETURNING id"), {"uid": token, "org": org_id}).scalar_one()
            plot_id = conn.execute(text("INSERT INTO estate_plots (plot_uid,estate_id,plot_number,plot_number_normalized,geometry,area_sqm,created_by_subject_type,created_by_subject_id) VALUES (:uid,:estate,'B-024','B-024',ST_GeomFromText('POLYGON((3 6,3.001 6,3.001 6.001,3 6))',4326),100,'test','owner') RETURNING id"), {"uid": token, "estate": estate_id}).scalar_one()
            customer_id = conn.execute(text("INSERT INTO estate_customers (customer_uid,organization_id,full_name,full_name_normalized) VALUES (:uid,:org,'Musa Ibrahim','MUSA IBRAHIM') RETURNING id"), {"uid": token, "org": org_id}).scalar_one()
            conn.execute(text("INSERT INTO estate_allocations (allocation_uid,organization_id,estate_id,plot_id,customer_id,status,created_by_subject_type,created_by_subject_id) VALUES (:uid,:org,:estate,:plot,:customer,'allocated','test','owner')"), {"uid": token, "org": org_id,"estate":estate_id,"plot":plot_id,"customer":customer_id})
            with pytest.raises(Exception):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO estate_allocations (allocation_uid,organization_id,estate_id,plot_id,customer_id,status,created_by_subject_type,created_by_subject_id) VALUES (:uid,:org,:estate,:plot,:customer,'reserved','test','owner')"), {"uid": uuid4().hex, "org": org_id,"estate":estate_id,"plot":plot_id,"customer":customer_id})
            conn.execute(text("INSERT INTO estate_audit_events (event_uid,organization_id,action,entity_type,entity_id) VALUES (:uid,:org,'test.created','test','1')"), {"uid": token, "org":org_id})
            with pytest.raises(Exception):
                with conn.begin_nested():
                    conn.execute(text("UPDATE estate_audit_events SET action='changed' WHERE event_uid=:uid"), {"uid": token})
    finally:
        engine.dispose()
