import os

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from app.db import engine, pool_metrics_enabled, pool_metrics_snapshot

router = APIRouter(prefix="/health", tags=["health"])

@router.get("/")
def health_check():
    return {"status": "healthy"}


@router.get("/pool")
def pool_diagnostics(request: Request):
    """Expose non-sensitive pool diagnostics only for an explicitly enabled non-production run."""
    environment = str(os.getenv("LANDCHECK_ENV", "")).lower()
    if not pool_metrics_enabled() or environment in {"production", "prod"}:
        raise HTTPException(status_code=404, detail="Not found")
    expected_token = os.getenv("LANDCHECK_POOL_METRICS_TOKEN")
    if not expected_token or request.headers.get("X-Landcheck-Pool-Diagnostics") != expected_token:
        raise HTTPException(status_code=404, detail="Not found")

    metrics = pool_metrics_snapshot()
    capacity = int(metrics["pool"]["capacity"])
    if metrics["pool"]["checked_out"] >= capacity:
        metrics["database"] = {"available": False, "reason": "pool_at_capacity"}
        return metrics

    if engine.dialect.name != "postgresql":
        metrics["database"] = {"available": False, "reason": "postgresql_only"}
        return metrics

    try:
        with engine.connect() as connection:
            activity = connection.execute(text("""
                SELECT
                    count(*) FILTER (WHERE state = 'active') AS active_sessions,
                    count(*) FILTER (WHERE state = 'active' AND wait_event IS NOT NULL) AS waiting_sessions,
                    count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction,
                    coalesce(max(EXTRACT(epoch FROM (clock_timestamp() - xact_start)) * 1000)
                        FILTER (WHERE xact_start IS NOT NULL), 0) AS longest_transaction_ms
                FROM pg_stat_activity
                WHERE datname = current_database()
            """)).mappings().one()
            locks = connection.execute(text("""
                SELECT
                    count(*) FILTER (WHERE NOT locks.granted) AS waiting_locks,
                    count(*) FILTER (WHERE locks.granted) AS granted_locks
                FROM pg_locks AS locks
                JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
                WHERE activity.datname = current_database()
            """)).mappings().one()
        metrics["database"] = {
            "available": True,
            "active_sessions": int(activity["active_sessions"] or 0),
            "waiting_sessions": int(activity["waiting_sessions"] or 0),
            "idle_in_transaction": int(activity["idle_in_transaction"] or 0),
            "longest_transaction_ms": round(float(activity["longest_transaction_ms"] or 0), 3),
            "waiting_locks": int(locks["waiting_locks"] or 0),
            "granted_locks": int(locks["granted_locks"] or 0),
        }
    except Exception:
        metrics["database"] = {"available": False, "reason": "diagnostic_query_failed"}
    return metrics
