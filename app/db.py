from collections import deque
from threading import Lock
from time import perf_counter

from sqlalchemy import create_engine, event
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


_POOL_METRICS_ENABLED = _env_bool("LANDCHECK_POOL_METRICS", False)
_POOL_METRICS_LOCK = Lock()
_POOL_WAIT_SAMPLES_MS = deque(maxlen=5000)
_POOL_HOLD_SAMPLES_MS = deque(maxlen=5000)
_POOL_METRICS = {
    "checkout_count": 0,
    "pool_timeout_count": 0,
    "peak_checked_out": 0,
    "query_count": 0,
    "query_time_ms": 0.0,
    "query_max_ms": 0.0,
}


def _record_pool_wait(duration_ms: float) -> None:
    with _POOL_METRICS_LOCK:
        _POOL_WAIT_SAMPLES_MS.append(duration_ms)


def _record_pool_timeout() -> None:
    with _POOL_METRICS_LOCK:
        _POOL_METRICS["pool_timeout_count"] += 1


def _record_query(duration_ms: float) -> None:
    with _POOL_METRICS_LOCK:
        _POOL_METRICS["query_count"] += 1
        _POOL_METRICS["query_time_ms"] += duration_ms
        _POOL_METRICS["query_max_ms"] = max(_POOL_METRICS["query_max_ms"], duration_ms)


def _distribution(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_ms": round(ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.50)))], 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))], 3),
        "max_ms": round(ordered[-1], 3),
    }


class InstrumentedQueuePool(QueuePool):
    """QueuePool that records checkout wait and timeout events when diagnostics are enabled."""

    def _do_get(self):  # type: ignore[override]
        started_at = perf_counter()
        try:
            return super()._do_get()
        except SQLAlchemyTimeoutError:
            _record_pool_timeout()
            raise
        finally:
            _record_pool_wait((perf_counter() - started_at) * 1000)


def _on_pool_checkout(_dbapi_connection, connection_record, _connection_proxy) -> None:
    checked_out_at = perf_counter()
    connection_record.info["landcheck_checked_out_at"] = checked_out_at
    with _POOL_METRICS_LOCK:
        _POOL_METRICS["checkout_count"] += 1
        pool = getattr(connection_record, "pool", None) or getattr(_connection_proxy, "_pool", None)
        try:
            _POOL_METRICS["peak_checked_out"] = max(
                int(_POOL_METRICS["peak_checked_out"]),
                int(pool.checkedout()) if pool is not None else 0,
            )
        except (AttributeError, TypeError):
            pass


def _on_pool_checkin(_dbapi_connection, connection_record) -> None:
    checked_out_at = connection_record.info.pop("landcheck_checked_out_at", None)
    if checked_out_at is not None:
        hold_ms = (perf_counter() - checked_out_at) * 1000
        with _POOL_METRICS_LOCK:
            _POOL_HOLD_SAMPLES_MS.append(hold_ms)


def _on_before_cursor_execute(_connection, _cursor, _statement, _parameters, context, _executemany) -> None:
    context.landcheck_query_started_at = perf_counter()


def _on_after_cursor_execute(_connection, _cursor, _statement, _parameters, context, _executemany) -> None:
    started_at = getattr(context, "landcheck_query_started_at", None)
    if started_at is not None:
        _record_query((perf_counter() - started_at) * 1000)


def attach_pool_instrumentation(target_engine) -> None:
    if getattr(target_engine, "_landcheck_pool_instrumented", False):
        return
    event.listen(target_engine, "checkout", _on_pool_checkout)
    event.listen(target_engine, "checkin", _on_pool_checkin)
    event.listen(target_engine, "before_cursor_execute", _on_before_cursor_execute)
    event.listen(target_engine, "after_cursor_execute", _on_after_cursor_execute)
    target_engine._landcheck_pool_instrumented = True


def pool_metrics_enabled() -> bool:
    return _POOL_METRICS_ENABLED


def reset_pool_metrics() -> None:
    with _POOL_METRICS_LOCK:
        _POOL_WAIT_SAMPLES_MS.clear()
        _POOL_HOLD_SAMPLES_MS.clear()
        _POOL_METRICS.update({
            "checkout_count": 0,
            "pool_timeout_count": 0,
            "peak_checked_out": 0,
            "query_count": 0,
            "query_time_ms": 0.0,
            "query_max_ms": 0.0,
        })


def pool_metrics_snapshot(target_engine=None) -> dict:
    target_engine = target_engine or engine
    pool = target_engine.pool
    try:
        checked_out = int(pool.checkedout())
        idle_pooled = int(pool.checkedin())
        overflow = int(pool.overflow())
        pool_size = int(pool.size())
        max_overflow = int(getattr(pool, "_max_overflow", 0))
        pool_timeout = float(getattr(pool, "_timeout", 0))
    except AttributeError:
        checked_out = idle_pooled = overflow = pool_size = max_overflow = 0
        pool_timeout = 0.0
    with _POOL_METRICS_LOCK:
        wait_samples = list(_POOL_WAIT_SAMPLES_MS)
        hold_samples = list(_POOL_HOLD_SAMPLES_MS)
        metrics = dict(_POOL_METRICS)
        metrics["peak_checked_out"] = max(int(metrics["peak_checked_out"]), checked_out)
    return {
        "enabled": _POOL_METRICS_ENABLED,
        "pool": {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "capacity": pool_size + max_overflow,
            "pool_timeout_seconds": pool_timeout,
            "checked_out": checked_out,
            "idle_pooled": idle_pooled,
            "overflow": overflow,
        },
        "counters": {
            "checkout_count": int(metrics["checkout_count"]),
            "pool_timeout_count": int(metrics["pool_timeout_count"]),
            "peak_checked_out": int(metrics["peak_checked_out"]),
            "query_count": int(metrics["query_count"]),
            "query_time_ms": round(float(metrics["query_time_ms"]), 3),
            "query_max_ms": round(float(metrics["query_max_ms"]), 3),
        },
        "checkout_wait_ms": _distribution(wait_samples),
        "connection_hold_ms": _distribution(hold_samples),
    }


# Server-side safety nets so one stuck request can never hold the whole pool hostage:
# - lock_timeout: a statement waiting on another transaction's lock gives up (and the request
#   fails fast) instead of queueing while holding a pooled connection;
# - idle_in_transaction_session_timeout: a connection whose owner went quiet inside an open
#   transaction is closed by Postgres, releasing its locks.
_connect_args = {}
if str(DATABASE_URL or "").startswith(("postgresql", "postgres")):
    _lock_ms = max(_env_int("DB_LOCK_TIMEOUT_MS", 20000), 0)
    _idle_tx_ms = max(_env_int("DB_IDLE_TX_TIMEOUT_MS", 300000), 0)
    _connect_args["options"] = f"-c lock_timeout={_lock_ms} -c idle_in_transaction_session_timeout={_idle_tx_ms}"

_engine_options = {
    "connect_args": _connect_args,
    "echo": _env_bool("SQLALCHEMY_ECHO", False),
    "pool_pre_ping": _env_bool("SQLALCHEMY_POOL_PRE_PING", True),
    "pool_recycle": max(_env_int("SQLALCHEMY_POOL_RECYCLE", 1800), 60),
    "pool_size": max(_env_int("SQLALCHEMY_POOL_SIZE", 10), 1),
    "max_overflow": max(_env_int("SQLALCHEMY_MAX_OVERFLOW", 20), 0),
    "pool_timeout": max(_env_int("SQLALCHEMY_POOL_TIMEOUT", 30), 5),
    "pool_use_lifo": _env_bool("SQLALCHEMY_POOL_USE_LIFO", True),
}
if _POOL_METRICS_ENABLED and str(DATABASE_URL or "").startswith(("postgresql", "postgres")):
    _engine_options["poolclass"] = InstrumentedQueuePool

engine = create_engine(DATABASE_URL, **_engine_options)
if _POOL_METRICS_ENABLED:
    attach_pool_instrumentation(engine)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
