"""Dedicated PostgreSQL-backed worker for Survey export jobs.

The API owns job creation and download authorization.  This process only claims queued jobs,
renders them through the existing Survey functions, and records fenced state transitions.  It is
intentionally conservative: one process and one export at a time by default.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.metrics import cleanup_worker_metrics
from app.routers import plots
from app.utils.row_security import set_survey_user_context

try:
    from prometheus_client import Counter, Gauge, Histogram

    WORKER_UP = Gauge(
        "landcheck_survey_export_worker_up",
        "Whether the Survey export worker process is alive.",
        multiprocess_mode="livesum",
    )
    JOBS = Gauge(
        "landcheck_survey_export_jobs",
        "Survey export jobs by status.",
        ("status",),
        multiprocess_mode="livesum",
    )
    OLDEST_QUEUED_AGE = Gauge(
        "landcheck_survey_export_oldest_queued_age_seconds",
        "Age of the oldest queued Survey export job.",
        multiprocess_mode="livesum",
    )
    DURATION = Histogram(
        "landcheck_survey_export_job_duration_seconds",
        "Survey export execution duration in seconds.",
        ("export_type", "outcome"),
        buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200),
    )
    RETRIES = Counter(
        "landcheck_survey_export_retries_total",
        "Survey export jobs returned to the queue for another attempt.",
        ("export_type",),
    )
    LEASE_RECOVERIES = Counter(
        "landcheck_survey_export_lease_recoveries_total",
        "Survey export jobs recovered after an expired lease.",
    )
except Exception:  # pragma: no cover - metrics must never stop the worker
    WORKER_UP = JOBS = OLDEST_QUEUED_AGE = DURATION = RETRIES = LEASE_RECOVERIES = None


logger = logging.getLogger("landcheck.survey_export_worker")

_EXPORT_TYPES = {
    "survey-plan.pdf",
    "orthophoto.pdf",
    "topomap.pdf",
    "survey-plan.dxf",
    "survey-plan.shapefile",
    "back-computation.pdf",
    "technical-report.docx",
    "subdivision_batch_survey_plans",
    "subdivision_batch_clean_copy_pdf",
}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
POLL_SECONDS = _env_float("SURVEY_EXPORT_WORKER_POLL_SECONDS", 2.0)
CONCURRENCY = _env_int("SURVEY_EXPORT_WORKER_CONCURRENCY", 1, 1)
LEASE_SECONDS = _env_int("SURVEY_EXPORT_WORKER_LEASE_SECONDS", 900, 30)
HEARTBEAT_SECONDS = min(_env_int("SURVEY_EXPORT_WORKER_HEARTBEAT_SECONDS", 30, 5), LEASE_SECONDS // 2)
MAX_ATTEMPTS = _env_int("SURVEY_EXPORT_WORKER_MAX_ATTEMPTS", 3, 1)
HEALTH_FILE = "/tmp/landcheck-survey-export-worker.health"


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    export_type: str
    lease_token: str
    attempt_count: int
    max_attempts: int
    owner_user_id: int | None


def _refresh_queue_metrics(db) -> None:
    if JOBS is None:
        return
    try:
        rows = db.execute(
            text(
                """
                SELECT status, COUNT(*) AS count
                FROM plot_export_jobs
                GROUP BY status
                """
            )
        ).mappings().all()
        for status in ("queued", "running", "completed", "failed"):
            JOBS.labels(status).set(0)
        for row in rows:
            status = str(row["status"] or "unknown")
            if status in {"queued", "running", "completed", "failed"}:
                JOBS.labels(status).set(int(row["count"] or 0))
        age = db.execute(
            text(
                """
                SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(created_at))), 0)
                FROM plot_export_jobs
                WHERE status = 'queued'
                """
            )
        ).scalar()
        OLDEST_QUEUED_AGE.set(max(float(age or 0), 0.0))
    except Exception:
        logger.debug("Unable to refresh Survey export worker metrics", exc_info=True)


def _recover_expired_leases(db) -> int:
    rows = db.execute(
        text(
            """
            UPDATE plot_export_jobs
            SET status = CASE
                            WHEN COALESCE(attempt_count, 0) < COALESCE(max_attempts, :default_max_attempts)
                              THEN 'queued'
                            ELSE 'failed'
                         END,
                available_at = NOW(),
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                completed_at = CASE
                                  WHEN COALESCE(attempt_count, 0) >= COALESCE(max_attempts, :default_max_attempts)
                                    THEN NOW()
                                  ELSE completed_at
                               END,
                error_text = 'Export lease expired; recovered after worker interruption.',
                last_error_code = 'lease_expired',
                updated_at = NOW()
            WHERE status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < NOW()
            RETURNING id
            """
        ),
        {"default_max_attempts": MAX_ATTEMPTS},
    ).scalars().all()
    db.commit()
    recovered = len(rows)
    if recovered and LEASE_RECOVERIES is not None:
        LEASE_RECOVERIES.inc(recovered)
    return recovered


def _touch_health_file() -> None:
    try:
        with open(HEALTH_FILE, "a", encoding="utf-8"):
            os.utime(HEALTH_FILE, None)
    except OSError:
        logger.debug("Unable to update worker health file", exc_info=True)


def _candidate_ids(db) -> list[str]:
    """Return an interleaved oldest-first list keyed by Survey owner.

    Survey currently scopes records by user rather than organization.  Interleaving owner queues
    prevents one user with many exports from monopolizing the single worker while preserving FIFO
    order within each owner.
    """
    rows = db.execute(
        text(
            """
            SELECT id
            FROM (
                SELECT
                    id,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(owner_user_id, 0)
                        ORDER BY created_at ASC, id ASC
                    ) AS owner_position
                FROM plot_export_jobs
                WHERE status = 'queued'
                  AND COALESCE(available_at, NOW()) <= NOW()
            ) eligible
            ORDER BY owner_position ASC, created_at ASC, id ASC
            LIMIT 16
            """
        )
    ).scalars().all()
    return [str(value) for value in rows]


def claim_next_job(db) -> ClaimedJob | None:
    """Atomically claim one queued job with a lease and fencing token."""
    for job_id in _candidate_ids(db):
        row = db.execute(
            text(
                """
                SELECT id, export_type, owner_user_id,
                       COALESCE(attempt_count, 0) AS attempt_count,
                       COALESCE(max_attempts, :default_max_attempts) AS max_attempts
                FROM plot_export_jobs
                WHERE id = :job_id
                  AND status = 'queued'
                  AND COALESCE(available_at, NOW()) <= NOW()
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"job_id": job_id, "default_max_attempts": MAX_ATTEMPTS},
        ).mappings().first()
        if not row:
            continue

        export_type = str(row["export_type"] or "")
        if export_type not in _EXPORT_TYPES:
            db.rollback()
            raise RuntimeError(f"Unsupported Survey export type in queue: {export_type}")

        token = uuid.uuid4().hex
        claimed = db.execute(
            text(
                """
                UPDATE plot_export_jobs
                SET status = 'running',
                    attempt_count = COALESCE(attempt_count, 0) + 1,
                    max_attempts = COALESCE(max_attempts, :default_max_attempts),
                    lease_owner = :lease_owner,
                    lease_token = :lease_token,
                    lease_expires_at = NOW() + make_interval(secs => :lease_seconds),
                    heartbeat_at = NOW(),
                    started_at = COALESCE(started_at, NOW()),
                    progress = 0,
                    error_text = NULL,
                    last_error_code = NULL,
                    updated_at = NOW()
                WHERE id = :job_id AND status = 'queued'
                RETURNING id, export_type, owner_user_id, attempt_count, max_attempts
                """
            ),
            {
                "job_id": job_id,
                "lease_owner": WORKER_ID,
                "lease_token": token,
                "lease_seconds": LEASE_SECONDS,
                "default_max_attempts": MAX_ATTEMPTS,
            },
        ).mappings().first()
        if claimed:
            db.commit()
            return ClaimedJob(
                id=str(claimed["id"]),
                export_type=str(claimed["export_type"]),
                lease_token=token,
                attempt_count=int(claimed["attempt_count"] or 0),
                max_attempts=int(claimed["max_attempts"] or MAX_ATTEMPTS),
                owner_user_id=int(claimed["owner_user_id"]) if claimed["owner_user_id"] is not None else None,
            )
        db.rollback()
    db.rollback()
    return None


def _heartbeat(job: ClaimedJob, stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_SECONDS):
        db = SessionLocal()
        try:
            db.execute(
                text(
                    """
                    UPDATE plot_export_jobs
                    SET heartbeat_at = NOW(),
                        lease_expires_at = NOW() + make_interval(secs => :lease_seconds),
                        updated_at = NOW()
                    WHERE id = :job_id
                      AND status = 'running'
                      AND lease_token = :lease_token
                    """
                ),
                {
                    "job_id": job.id,
                    "lease_token": job.lease_token,
                    "lease_seconds": LEASE_SECONDS,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("Survey export heartbeat failed for job %s", job.id)
        finally:
            db.close()


def _is_cancel_requested(job: ClaimedJob) -> bool:
    db = SessionLocal()
    try:
        value = db.execute(
            text(
                """
                SELECT cancel_requested_at IS NOT NULL
                FROM plot_export_jobs
                WHERE id = :job_id AND lease_token = :lease_token
                """
            ),
            {"job_id": job.id, "lease_token": job.lease_token},
        ).scalar()
        return bool(value)
    finally:
        db.close()


def _finish_cancelled(job: ClaimedJob) -> None:
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE plot_export_jobs
                SET status = 'failed',
                    error_text = 'Export cancelled by the user.',
                    last_error_code = 'cancelled',
                    completed_at = NOW(),
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    updated_at = NOW()
                WHERE id = :job_id AND lease_token = :lease_token
                """
            ),
            {"job_id": job.id, "lease_token": job.lease_token},
        )
        db.commit()
    finally:
        db.close()


def _retry_or_fail(job: ClaimedJob, exc: Exception) -> None:
    db = SessionLocal()
    try:
        current = db.execute(
            text(
                """
                SELECT attempt_count, max_attempts, export_type, cancel_requested_at
                FROM plot_export_jobs
                WHERE id = :job_id AND lease_token = :lease_token
                FOR UPDATE
                """
            ),
            {"job_id": job.id, "lease_token": job.lease_token},
        ).mappings().first()
        if not current:
            db.rollback()
            return
        if current["cancel_requested_at"] is not None:
            db.execute(
                text(
                    """
                    UPDATE plot_export_jobs
                    SET status = 'failed', error_text = 'Export cancelled by the user.',
                        last_error_code = 'cancelled', completed_at = NOW(),
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        heartbeat_at = NULL, updated_at = NOW()
                    WHERE id = :job_id AND lease_token = :lease_token
                    """
                ),
                {"job_id": job.id, "lease_token": job.lease_token},
            )
            db.commit()
            return

        attempt_count = int(current["attempt_count"] or 0)
        max_attempts = int(current["max_attempts"] or MAX_ATTEMPTS)
        retryable = isinstance(exc, (OperationalError, TimeoutError, ConnectionError, OSError))
        retryable = retryable or any(
            marker in str(exc).lower()
            for marker in ("timed out", "timeout", "temporarily unavailable", "connection reset", "tile")
        )
        if retryable and attempt_count < max_attempts:
            delay_seconds = min(600, 30 * (2 ** max(0, attempt_count - 1)))
            db.execute(
                text(
                    """
                    UPDATE plot_export_jobs
                    SET status = 'queued',
                        available_at = NOW() + make_interval(secs => :delay_seconds),
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        heartbeat_at = NULL, progress = 0,
                        error_text = :error_text, last_error_code = 'retryable_failure',
                        updated_at = NOW()
                    WHERE id = :job_id AND lease_token = :lease_token
                    """
                ),
                {
                    "job_id": job.id,
                    "lease_token": job.lease_token,
                    "delay_seconds": delay_seconds,
                    "error_text": str(exc)[:2000],
                },
            )
            db.commit()
            if RETRIES is not None:
                RETRIES.labels(job.export_type).inc()
            return

        db.execute(
            text(
                """
                UPDATE plot_export_jobs
                SET status = 'failed', completed_at = NOW(),
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, error_text = :error_text,
                    last_error_code = :last_error_code, updated_at = NOW()
                WHERE id = :job_id AND lease_token = :lease_token
                """
            ),
            {
                "job_id": job.id,
                "lease_token": job.lease_token,
                "error_text": str(exc)[:2000],
                "last_error_code": "retry_exhausted" if retryable else "permanent_failure",
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Unable to record Survey export failure for job %s", job.id)
    finally:
        db.close()


def _run_claimed_job(job: ClaimedJob) -> None:
    started = time.perf_counter()
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    try:
        if job.owner_user_id is None:
            raise RuntimeError("Survey export job has no owner scope.")
        if _is_cancel_requested(job):
            _finish_cancelled(job)
            return
        heartbeat_thread = threading.Thread(target=_heartbeat, args=(job, heartbeat_stop), daemon=True)
        heartbeat_thread.start()
        if job.export_type == "subdivision_batch_survey_plans":
            plots._run_subdivision_batch_export_job(job.id, lease_token=job.lease_token, mark_failure=False)
        elif job.export_type == "subdivision_batch_clean_copy_pdf":
            plots._run_subdivision_batch_clean_copy_export_job(job.id, lease_token=job.lease_token, mark_failure=False)
        else:
            plots._run_single_plot_export_job(job.id, lease_token=job.lease_token, mark_failure=False)
        if DURATION is not None:
            DURATION.labels(job.export_type, "completed").observe(time.perf_counter() - started)
    except Exception as exc:
        _retry_or_fail(job, exc)
        if DURATION is not None:
            DURATION.labels(job.export_type, "failed").observe(time.perf_counter() - started)
        logger.warning("Survey export job %s failed: %s", job.id, exc)
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)


def run_worker(stop_event: threading.Event | None = None) -> None:
    stop_event = stop_event or threading.Event()
    if CONCURRENCY != 1:
        raise RuntimeError("Phase 1 Survey export worker supports exactly one concurrent export.")
    if WORKER_UP is not None:
        WORKER_UP.set(1)
    try:
        while not stop_event.is_set():
            _touch_health_file()
            db = SessionLocal()
            try:
                _recover_expired_leases(db)
                _refresh_queue_metrics(db)
                job = claim_next_job(db)
            except Exception:
                db.rollback()
                job = None
                logger.exception("Survey export worker database cycle failed")
            finally:
                db.close()
            if job:
                _run_claimed_job(job)
                continue
            stop_event.wait(POLL_SECONDS)
    finally:
        if WORKER_UP is not None:
            WORKER_UP.set(0)
        cleanup_worker_metrics()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Survey export worker started: %s", WORKER_ID)
    run_worker(stop_event)
    logger.info("Survey export worker stopped")


if __name__ == "__main__":
    main()
