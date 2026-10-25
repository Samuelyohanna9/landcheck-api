"""Focused Phase 4 checks for the operations-summary batching change.

These tests require an explicitly supplied isolated PostgreSQL database:
PHASE4_DATABASE_URL=postgresql+psycopg2://... pytest tests/test_estate_operations_phase4.py
"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import sessionmaker


DATABASE_URL = os.getenv("PHASE4_DATABASE_URL")
if not DATABASE_URL:
    pytest.skip("set PHASE4_DATABASE_URL to run Phase 4 integration checks", allow_module_level=True)
if not DATABASE_URL.startswith("postgresql"):
    pytest.skip("Phase 4 checks require the isolated PostgreSQL fixture", allow_module_level=True)

from app.models.estate_foundation import (  # noqa: E402
    Estate,
    EstateAllocation,
    EstateAuditEvent,
    EstateCommissionPayout,
    EstateDocument,
    EstateDocumentLink,
    EstatePayment,
    EstatePaymentInbox,
    EstatePlot,
    EstateStakingTask,
    EstateSurveyRequest,
)
from app.services.estates import operations  # noqa: E402
from app.services.estates.operations import (  # noqa: E402
    build_operations_summary,
    document_readiness,
    document_readiness_batch,
)
from app.services.estates.payments import (  # noqa: E402
    FinancialSummary,
    financial_summaries,
    financial_summary,
)


@pytest.fixture(scope="module")
def db_session():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _legacy_operations_summary(db, estate):
    now = operations._now()
    allocations = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate.id).all()
    active_allocations = [row for row in allocations if row.status in {"reserved", "allocated"}]
    surveys = {
        row.plot_id: row
        for row in db.query(EstateSurveyRequest).filter(EstateSurveyRequest.estate_id == estate.id).all()
    }
    staking = {
        row.plot_id: row
        for row in db.query(EstateStakingTask).filter(EstateStakingTask.estate_id == estate.id).all()
    }
    summaries = {row.id: financial_summary(db, row) for row in active_allocations}
    missing_documents = sum(1 for row in active_allocations if not document_readiness(db, row)["complete"])
    payment_overdue = sum(
        1 for row in active_allocations if operations._has_overdue_installment(row, summaries[row.id], now)
    )
    expiring = sum(
        1
        for row in active_allocations
        if row.status == "reserved"
        and row.reservation_expires_at
        and now <= row.reservation_expires_at <= now + timedelta(hours=72)
    )
    allocated_not_surveyed = sum(
        1
        for row in active_allocations
        if row.status == "allocated"
        and (not surveys.get(row.plot_id) or surveys[row.plot_id].status not in {"completed", "approved"})
    )
    survey_not_staked = sum(
        1
        for row in surveys.values()
        if row.status in {"completed", "approved"}
        and (not staking.get(row.plot_id) or staking[row.plot_id].status not in {"completed", "approved"})
    )
    commission_paid = {
        allocation_id: Decimal(str(total or 0))
        for allocation_id, total in db.query(
            EstateCommissionPayout.allocation_id,
            func.coalesce(func.sum(EstateCommissionPayout.amount), 0),
        )
        .filter(EstateCommissionPayout.allocation_id.in_([row.id for row in active_allocations] or [-1]))
        .group_by(EstateCommissionPayout.allocation_id)
        .all()
    }
    commission_pending = sum(
        1
        for row in active_allocations
        if row.status == "allocated"
        and row.commission_amount
        and Decimal(str(row.commission_amount)) > commission_paid.get(row.id, Decimal("0"))
    )
    unmatched_payments = (
        db.query(EstatePaymentInbox)
        .filter(
            EstatePaymentInbox.organization_id == estate.organization_id,
            EstatePaymentInbox.status == "unmatched",
        )
        .count()
    )
    latest_plot_update = db.query(func.max(EstatePlot.updated_at)).filter(EstatePlot.estate_id == estate.id).scalar()
    latest_public_update = db.query(func.max(EstateAuditEvent.created_at)).filter(
        EstateAuditEvent.organization_id == estate.organization_id,
        EstateAuditEvent.entity_type == "estate",
        EstateAuditEvent.entity_id == str(estate.id),
        EstateAuditEvent.action.in_(
            ("estate.public_settings.updated", "public_page.published", "estate.public_logo_updated")
        ),
    ).scalar()
    public_outdated = bool(
        estate.public_enabled
        and latest_plot_update
        and (not latest_public_update or latest_plot_update > latest_public_update)
    )
    actions = [
        {"key": "payment_overdue", "label": "Payments overdue", "count": payment_overdue, "href": "/estates/payments", "tone": "danger"},
        {"key": "reservation_expiring", "label": "Reservations expiring", "count": expiring, "href": f"/estates/{estate.id}/customers", "tone": "warn"},
        {"key": "documents_missing", "label": "Documents missing", "count": missing_documents, "href": "/estates/documents", "tone": "warn"},
        {"key": "allocated_not_surveyed", "label": "Allocated but not surveyed", "count": allocated_not_surveyed, "href": f"/estates/{estate.id}/survey", "tone": "info"},
        {"key": "survey_not_staked", "label": "Survey completed but not staked", "count": survey_not_staked, "href": f"/estates/{estate.id}/staking", "tone": "info"},
        {"key": "commission_pending", "label": "Commission awaiting approval", "count": commission_pending, "href": "/estates/commissions", "tone": "warn"},
        {"key": "public_outdated", "label": "Public page data outdated", "count": 1 if public_outdated else 0, "href": f"/estates/{estate.id}/settings", "tone": "info"},
        {"key": "unmatched_payments", "label": "Unmatched payments", "count": unmatched_payments, "href": "/estates/reconciliation", "tone": "danger"},
    ]
    return {
        "estate_id": estate.id,
        "generated_at": now,
        "counts": {item["key"]: item["count"] for item in actions},
        "actions": [item for item in actions if item["count"]],
        "total_exceptions": sum(item["count"] for item in actions),
    }


def _without_timestamp(value):
    if isinstance(value, dict):
        return {key: _without_timestamp(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_without_timestamp(item) for item in value]
    return value


def test_summary_matches_previous_implementation_across_estates(db_session, monkeypatch):
    fixed_now = operations._now()
    monkeypatch.setattr(operations, "_now", lambda: fixed_now)

    for estate_id in (1, 2, 3, 4, 5, 6):
        legacy_db = db_session
        estate = legacy_db.get(Estate, estate_id)
        expected = _without_timestamp(_legacy_operations_summary(legacy_db, estate))
        actual = _without_timestamp(build_operations_summary(legacy_db, estate))
        assert actual == expected, f"optimized summary differs for estate {estate_id}"


def test_summary_query_count_is_constant_for_allocation_sizes(db_session):
    engine = db_session.get_bind()
    counts = []
    statements = {"count": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def count_queries(conn, cursor, statement, parameters, context, executemany):
        statements["count"] += 1

    try:
        for estate_id in (1, 2, 3):
            estate = db_session.get(Estate, estate_id)
            statements["count"] = 0
            build_operations_summary(db_session, estate)
            counts.append(statements["count"])
    finally:
        event.remove(engine, "before_cursor_execute", count_queries)

    assert counts == [10, 10, 10]


def test_batched_financials_and_readiness_cover_edge_cases(db_session):
    partial = db_session.get(EstateAllocation, 1)
    missing_documents = db_session.get(EstateAllocation, 4)
    assert partial is not None and missing_documents is not None

    partial_summary = financial_summaries(db_session, [partial])[partial.id]
    assert partial_summary.confirmed_paid == Decimal("2500000.00")
    assert partial_summary.outstanding == Decimal("2500000.00")

    readiness = document_readiness_batch(db_session, [missing_documents])[missing_documents.id]
    assert readiness == document_readiness(db_session, missing_documents)
    assert readiness["complete"] is False
    assert financial_summaries(db_session, []) == {}
    assert document_readiness_batch(db_session, []) == {}

    fixed_now = operations._now()
    overdue = FinancialSummary(Decimal("100"), Decimal("50"), Decimal("0"), Decimal("50"), Decimal("50"))
    fully_paid = FinancialSummary(Decimal("100"), Decimal("100"), Decimal("0"), Decimal("0"), Decimal("100"))
    partial.next_payment_due_at = fixed_now - timedelta(days=1)
    assert operations._has_overdue_installment(partial, overdue, fixed_now) is True
    assert operations._has_overdue_installment(partial, fully_paid, fixed_now) is False
    db_session.rollback()
