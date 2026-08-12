from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

HAZARD_JOB_STATUS_VALUES = {"queued", "running", "completed", "failed"}

_HAZARD_JOBS_TABLE_LOCK = threading.Lock()
_HAZARD_JOBS_TABLE_READY = False


def ensure_hazard_analysis_jobs_table(db: Session) -> None:
    """Same lazy-once-per-process guard pattern as plots.py's ensure_plots_schema_once, so this
    DDL round-trip only happens on the first request that touches hazard jobs, not every request.
    """
    global _HAZARD_JOBS_TABLE_READY
    if _HAZARD_JOBS_TABLE_READY:
        return
    with _HAZARD_JOBS_TABLE_LOCK:
        if _HAZARD_JOBS_TABLE_READY:
            return
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS hazard_analysis_jobs (
                id TEXT PRIMARY KEY,
                hazard_type TEXT NOT NULL,
                output_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT,
                progress_pct INTEGER NOT NULL DEFAULT 0,
                request_payload JSONB,
                result_payload JSONB,
                file_bytes BYTEA,
                file_name TEXT,
                content_type TEXT,
                error_text TEXT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_hazard_analysis_jobs_status_created "
            "ON hazard_analysis_jobs(status, created_at DESC)"
        ))
        db.commit()
        _HAZARD_JOBS_TABLE_READY = True


def insert_hazard_job(
    db: Session,
    *,
    hazard_type: str,
    output_type: str,
    request_payload: Dict[str, Any],
    worker: Callable[[str], None],
) -> Dict[str, Any]:
    ensure_hazard_analysis_jobs_table(db)
    job_id = uuid.uuid4().hex
    db.execute(text("""
        INSERT INTO hazard_analysis_jobs (
            id, hazard_type, output_type, status, stage, progress_pct, request_payload, created_at, updated_at
        ) VALUES (
            :id, :hazard_type, :output_type, 'queued', 'Queued', 0, CAST(:request_payload AS JSONB), NOW(), NOW()
        )
    """), {
        "id": job_id,
        "hazard_type": hazard_type,
        "output_type": output_type,
        "request_payload": json.dumps(request_payload or {}),
    })
    db.commit()
    threading.Thread(target=worker, args=(job_id,), daemon=True).start()
    return get_hazard_job(db, job_id) or {"id": job_id, "status": "queued"}


def get_hazard_job(db: Session, job_id: str) -> Optional[Dict[str, Any]]:
    ensure_hazard_analysis_jobs_table(db)
    row = db.execute(text("""
        SELECT id, hazard_type, output_type, status, stage, progress_pct,
               request_payload, result_payload, file_name, content_type, error_text,
               started_at, completed_at, created_at, updated_at
        FROM hazard_analysis_jobs WHERE id = :job_id LIMIT 1
    """), {"job_id": str(job_id)}).mappings().first()
    return dict(row) if row else None


def get_hazard_job_file(db: Session, job_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute(text("""
        SELECT file_bytes, file_name, content_type, status
        FROM hazard_analysis_jobs WHERE id = :job_id LIMIT 1
    """), {"job_id": str(job_id)}).mappings().first()
    return dict(row) if row else None


def set_hazard_job_status(
    db: Session,
    job_id: str,
    *,
    status: str,
    stage: Optional[str] = None,
    progress_pct: Optional[int] = None,
    result_payload: Optional[Dict[str, Any]] = None,
    file_bytes: Optional[bytes] = None,
    file_name: Optional[str] = None,
    content_type: Optional[str] = None,
    error_text: Optional[str] = None,
    started: bool = False,
    completed: bool = False,
) -> None:
    if status not in HAZARD_JOB_STATUS_VALUES:
        raise ValueError(f"Unsupported hazard job status: {status}")
    updates = ["status = :status", "updated_at = NOW()"]
    params: Dict[str, Any] = {"job_id": str(job_id), "status": status}
    if stage is not None:
        updates.append("stage = :stage")
        params["stage"] = stage
    if progress_pct is not None:
        updates.append("progress_pct = :progress_pct")
        params["progress_pct"] = int(progress_pct)
    if result_payload is not None:
        updates.append("result_payload = CAST(:result_payload AS JSONB)")
        params["result_payload"] = json.dumps(result_payload)
    if file_bytes is not None:
        updates.append("file_bytes = :file_bytes")
        params["file_bytes"] = file_bytes
    if file_name is not None:
        updates.append("file_name = :file_name")
        params["file_name"] = file_name
    if content_type is not None:
        updates.append("content_type = :content_type")
        params["content_type"] = content_type
    if error_text is not None:
        updates.append("error_text = :error_text")
        params["error_text"] = error_text
    if started:
        updates.append("started_at = COALESCE(started_at, NOW())")
    if completed:
        updates.append("completed_at = NOW()")
    db.execute(text(f"UPDATE hazard_analysis_jobs SET {', '.join(updates)} WHERE id = :job_id"), params)
    db.commit()


def make_progress_reporter(db: Session, job_id: str) -> Callable[[str, int], None]:
    """Returns a (stage_text, pct) -> None callback that compute_flood_risk/compute_erosion_risk
    call at each phase, so a client polling the job can show real backend-driven progress instead
    of a fake/generic spinner.
    """
    def _report(stage: str, pct: int) -> None:
        try:
            set_hazard_job_status(db, job_id, status="running", stage=stage, progress_pct=pct)
        except Exception:
            pass
    return _report


def serialize_hazard_job(job: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(job)
    return {
        "id": item.get("id"),
        "hazard_type": item.get("hazard_type"),
        "output_type": item.get("output_type"),
        "status": item.get("status"),
        "stage": item.get("stage"),
        "progress_pct": item.get("progress_pct"),
        "error_text": item.get("error_text"),
        "result": item.get("result_payload"),
        "download_url": (
            f"/hazards/jobs/{item.get('id')}/download"
            if str(item.get("status")) == "completed" and item.get("output_type") in ("pdf", "gis-export")
            else None
        ),
    }
