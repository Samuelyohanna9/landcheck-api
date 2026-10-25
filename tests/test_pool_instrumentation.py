from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.db import (
    InstrumentedQueuePool,
    _on_after_cursor_execute,
    _on_before_cursor_execute,
    _on_pool_checkin,
    _on_pool_checkout,
    pool_metrics_snapshot,
    reset_pool_metrics,
)


@pytest.fixture
def instrumented_sqlite_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=InstrumentedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    event.listen(engine, "checkout", _on_pool_checkout)
    event.listen(engine, "checkin", _on_pool_checkin)
    event.listen(engine, "before_cursor_execute", _on_before_cursor_execute)
    event.listen(engine, "after_cursor_execute", _on_after_cursor_execute)
    reset_pool_metrics()
    try:
        yield engine
    finally:
        engine.dispose()
        reset_pool_metrics()


def test_pool_instrumentation_records_queries_and_hold_time(instrumented_sqlite_engine):
    with instrumented_sqlite_engine.connect() as connection:
        connection.execute(text("select 1"))

    snapshot = pool_metrics_snapshot(instrumented_sqlite_engine)
    assert snapshot["counters"]["checkout_count"] == 1
    assert snapshot["counters"]["query_count"] == 1
    assert snapshot["checkout_wait_ms"]["count"] == 1
    assert snapshot["connection_hold_ms"]["count"] == 1


def test_pool_instrumentation_records_checkout_timeout(instrumented_sqlite_engine):
    connection = instrumented_sqlite_engine.connect()
    try:
        with pytest.raises(SQLAlchemyTimeoutError):
            instrumented_sqlite_engine.connect()
    finally:
        connection.close()

    snapshot = pool_metrics_snapshot(instrumented_sqlite_engine)
    assert snapshot["counters"]["pool_timeout_count"] == 1
    assert snapshot["pool"]["checked_out"] == 0
