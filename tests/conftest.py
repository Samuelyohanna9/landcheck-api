from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.estate_foundation import (
    EstateAuditEvent,
    EstateOrganization,
    EstateOrganizationEntitlement,
    EstateOrganizationMember,
)
from app.models.estate_auth import EstateAccount
from app.models.estate_billing import EstateSubscription, EstateSubscriptionCharge


def _test_database_url() -> str:
    value = str(os.getenv("LANDCHECK_TEST_DATABASE_URL") or "sqlite+pysqlite:///:memory:").strip()
    if not value.startswith("sqlite") and "test" not in value.lower():
        raise RuntimeError("LANDCHECK_TEST_DATABASE_URL must point to a dedicated test database")
    return value


@pytest.fixture()
def db_session() -> Session:
    url = _test_database_url()
    options = {"future": True}
    if url.startswith("sqlite"):
        options.update({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool})
    engine = create_engine(url, **options)
    tables = [
        EstateOrganization.__table__,
        EstateOrganizationMember.__table__,
        EstateOrganizationEntitlement.__table__,
        EstateSubscription.__table__,
        EstateSubscriptionCharge.__table__,
        EstateAccount.__table__,
        EstateAuditEvent.__table__,
    ]
    for table in tables:
        table.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        for table in reversed(tables):
            table.drop(bind=engine, checkfirst=True)
        engine.dispose()
