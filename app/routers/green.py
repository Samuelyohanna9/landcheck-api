from fastapi import APIRouter, Body, Depends, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, date, timedelta
import json
import os
import tempfile
import csv
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import boto3
from botocore.exceptions import ClientError

from app.db import SessionLocal
from app.utils.green_pdf import render_green_report_pdf, render_green_work_report_pdf

router = APIRouter(prefix="/green", tags=["green"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports", "green")
LIVE_SOURCE_REFERENCES = [
    {
        "label": "FAO - Forest restoration monitoring and maintenance sequence",
        "url": "https://www.fao.org/sustainable-forest-management-toolbox/modules/forest-restoration/en",
    },
    {
        "label": "FAO - Post-planting operations (watering, protection, replacement)",
        "url": "https://www.fao.org/4/u2247e/u2247e0a.htm",
    },
    {
        "label": "FAO - Savanna plantation field maintenance practices (Nigeria-relevant context)",
        "url": "https://www.fao.org/4/93269e/93269e03.htm",
    },
    {
        "label": "NiMet seasonal outlook context for local onset/dry-period planning",
        "url": "https://www.nimet.gov.ng/news?id=94",
    },
]

MAINTENANCE_ACTIVITY_ORDER = ("watering", "weeding", "protection", "inspection", "replacement")
SEASON_VALUES = {"rainy", "dry"}
TASK_STATUS_VALUES = {"pending", "done", "overdue"}
REVIEW_STATE_VALUES = {"none", "submitted", "approved", "rejected", "reopened"}
TREE_STATUS_VALUES = {
    "alive",
    "healthy",
    "dead",
    "needs_attention",
    "pending_planting",
    "pest",
    "disease",
    "need_replacement",
    "needs_replacement",
    "damaged",
    "removed",
    "need_watering",
    "need_protection",
}
REPLACEMENT_TRIGGER_STATUSES = {"dead", "damaged", "removed", "need_replacement", "needs_replacement"}
TREE_STATUS_ALIASES = {
    "needreplacement": "need_replacement",
    "need_replacement": "need_replacement",
    "needsreplacement": "needs_replacement",
    "needs_replacement": "needs_replacement",
    "need replacement": "need_replacement",
    "needs replacement": "needs_replacement",
    "deseas": "disease",
    "diseased": "disease",
    "needsattention": "needs_attention",
    "need_attention": "needs_attention",
    "need_watering": "need_watering",
    "needwatering": "need_watering",
    "need watering": "need_watering",
    "need_protection": "need_protection",
    "needprotection": "need_protection",
    "need protection": "need_protection",
}
HEALTHY_TREE_STATUSES = {"alive", "healthy"}
DEAD_TREE_STATUSES = {"dead", "removed"}
ATTENTION_TREE_STATUSES = {
    "needs_attention",
    "pest",
    "disease",
    "need_replacement",
    "needs_replacement",
    "damaged",
    "need_watering",
    "need_protection",
}
TREE_STATUS_COLOR_HEX = {
    "alive": "22c55e",
    "healthy": "16a34a",
    "pest": "eab308",
    "disease": "f97316",
    "need_replacement": "ef4444",
    "needs_replacement": "ef4444",
    "damaged": "dc2626",
    "dead": "b91c1c",
    "removed": "7f1d1d",
    "needs_attention": "f59e0b",
    "pending_planting": "3b82f6",
    "need_watering": "0ea5e9",
    "need_protection": "a855f7",
}


def get_db():
    db = SessionLocal()
    try:
        ensure_green_tables(db)
        yield db
    finally:
        db.close()


def _normalize_name(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalize_tree_status(value: str | None) -> str:
    raw = _normalize_name(value).replace("-", "_")
    collapsed = raw.replace("_", "").replace(" ", "")
    if raw in TREE_STATUS_ALIASES:
        return TREE_STATUS_ALIASES[raw]
    if collapsed in TREE_STATUS_ALIASES:
        return TREE_STATUS_ALIASES[collapsed]
    if " " in raw:
        spaced = raw.replace(" ", "_")
        if spaced in TREE_STATUS_ALIASES:
            return TREE_STATUS_ALIASES[spaced]
    return raw.replace(" ", "_")


def _is_done_status(status: str | None) -> bool:
    return _normalize_name(status) in {"done", "completed", "closed"}


def _start_of_day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _parse_date_value(value: str | datetime | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if len(raw) >= 10:
            return date.fromisoformat(raw[:10])
        return date.fromisoformat(raw)
    except Exception:
        return None


def _to_date_input(value: date | None) -> str:
    return value.isoformat() if value else ""


def _add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def _day_diff(target: date, reference: date) -> int:
    return (_start_of_day(target) - _start_of_day(reference)).days


def _safe_json(value: dict | list | None) -> str:
    try:
        return json.dumps(value or {}, default=str)
    except Exception:
        return "{}"


def _get_maintenance_intervals(activity: str, tree_age_days: int, season: str) -> dict:
    activity_key = _normalize_name(activity)
    season_key = "dry" if _normalize_name(season) == "dry" else "rainy"
    age = max(int(tree_age_days or 0), 0)

    if activity_key == "watering":
        if season_key == "rainy":
            return {"first_days": 0, "repeat_days": 14 if age < 90 else 21}
        return {"first_days": 0, "repeat_days": 5 if age < 90 else 7}

    if activity_key == "weeding":
        if season_key == "rainy":
            if age < 365:
                return {"first_days": 21, "repeat_days": 45}
            if age < 730:
                return {"first_days": 30, "repeat_days": 90}
            return {"first_days": 30, "repeat_days": 150}
        if age < 365:
            return {"first_days": 35, "repeat_days": 90}
        if age < 730:
            return {"first_days": 45, "repeat_days": 150}
        return {"first_days": 45, "repeat_days": 210}

    if activity_key == "protection":
        if season_key == "rainy":
            return {"first_days": 0, "repeat_days": 45}
        return {"first_days": 0, "repeat_days": 21}

    if activity_key == "inspection":
        if season_key == "rainy":
            return {"first_days": 14, "repeat_days": 30 if age < 180 else 90}
        return {"first_days": 7, "repeat_days": 21 if age < 180 else 60}

    if activity_key == "replacement":
        if season_key == "rainy":
            return {"first_days": 42, "repeat_days": 180}
        return {"first_days": 56, "repeat_days": 210}

    return {"first_days": 30, "repeat_days": 90}


def _get_lifecycle_start_date(planting_date_obj: date | None, replacement_done_obj: date | None) -> date | None:
    if planting_date_obj and replacement_done_obj:
        return replacement_done_obj if replacement_done_obj > planting_date_obj else planting_date_obj
    return replacement_done_obj or planting_date_obj


def _get_project_id_for_tree(db: Session, tree_id: int) -> int | None:
    return db.execute(
        text("SELECT project_id FROM trees WHERE id = :tree_id"),
        {"tree_id": tree_id},
    ).scalar()


def _get_project_id_for_task(db: Session, task_id: int) -> int | None:
    return db.execute(
        text("""
            SELECT tr.project_id
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE t.id = :task_id
        """),
        {"task_id": task_id},
    ).scalar()


def _log_audit_event(
    db: Session,
    project_id: int | None,
    entity_type: str,
    entity_id: int | None,
    action: str,
    actor: str | None = None,
    details: dict | None = None,
):
    db.execute(
        text("""
            INSERT INTO green_audit_events (
                project_id, entity_type, entity_id, action, actor, details
            )
            VALUES (:project_id, :entity_type, :entity_id, :action, :actor, CAST(:details AS JSONB))
        """),
        {
            "project_id": project_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "actor": actor,
            "details": _safe_json(details),
        },
    )


def _get_species_maturity_map(project_id: int, db: Session) -> dict[str, int]:
    rows = db.execute(
        text("""
            SELECT species_key, maturity_years
            FROM green_species_maturity
            WHERE project_id = :project_id
        """),
        {"project_id": project_id},
    ).mappings().all()
    result: dict[str, int] = {}
    for row in rows:
        key = _normalize_name(row.get("species_key"))
        years = int(row.get("maturity_years") or 0)
        if key and years > 0:
            result[key] = years
    return result


def _task_needs_evidence(task_type: str | None) -> dict:
    # Premium default: every maintenance completion requires note + photo proof.
    activity = _normalize_name(task_type)
    if activity in MAINTENANCE_ACTIVITY_ORDER:
        return {"require_notes": True, "require_photo": True}
    return {"require_notes": False, "require_photo": False}


def _has_required_evidence(task_type: str | None, notes: str | None, photo_url: str | None) -> tuple[bool, str]:
    policy = _task_needs_evidence(task_type)
    notes_ok = bool((notes or "").strip())
    photo_ok = bool((photo_url or "").strip())
    if policy["require_notes"] and not notes_ok:
        return False, "Notes are required before submission."
    if policy["require_photo"] and not photo_ok:
        return False, "Photo proof is required before submission."
    return True, ""


def _is_replacement_trigger_status(status: str | None) -> bool:
    return _normalize_tree_status(status) in REPLACEMENT_TRIGGER_STATUSES


def _tree_status_color_hex(status: str | None) -> str:
    return TREE_STATUS_COLOR_HEX.get(_normalize_tree_status(status), "22c55e")


def _record_alert(
    db: Session,
    project_id: int,
    alert_type: str,
    severity: str,
    message: str,
    tree_id: int | None = None,
    task_id: int | None = None,
    payload: dict | None = None,
):
    existing = db.execute(
        text("""
            SELECT id
            FROM green_alerts
            WHERE project_id = :project_id
              AND COALESCE(tree_id, 0) = COALESCE(:tree_id, 0)
              AND COALESCE(task_id, 0) = COALESCE(:task_id, 0)
              AND alert_type = :alert_type
              AND status = 'open'
            LIMIT 1
        """),
        {
            "project_id": project_id,
            "tree_id": tree_id,
            "task_id": task_id,
            "alert_type": alert_type,
        },
    ).scalar()
    if existing:
        return existing
    return db.execute(
        text("""
            INSERT INTO green_alerts (
                project_id, tree_id, task_id, alert_type, severity, message, payload
            )
            VALUES (
                :project_id, :tree_id, :task_id, :alert_type, :severity, :message, CAST(:payload AS JSONB)
            )
            RETURNING id
        """),
        {
            "project_id": project_id,
            "tree_id": tree_id,
            "task_id": task_id,
            "alert_type": alert_type,
            "severity": severity,
            "message": message,
            "payload": _safe_json(payload),
        },
    ).scalar()


def _resolve_task_alerts(db: Session, task_id: int):
    db.execute(
        text("""
            UPDATE green_alerts
            SET status = 'resolved',
                resolved_at = NOW()
            WHERE task_id = :task_id
              AND status = 'open'
        """),
        {"task_id": task_id},
    )


def _refresh_project_alerts(db: Session, project_id: int):
    db.execute(
        text("""
            DELETE FROM green_alerts
            WHERE project_id = :project_id
              AND status = 'open'
              AND alert_type IN ('task_overdue', 'task_due_soon', 'task_submitted', 'missing_evidence')
        """),
        {"project_id": project_id},
    )

    rows = db.execute(
        text("""
            SELECT t.id AS task_id, t.tree_id, t.task_type, t.assignee_name, t.status, t.review_state,
                   t.due_date, t.notes, t.photo_url, t.completed_at,
                   tr.project_id
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE tr.project_id = :project_id
        """),
        {"project_id": project_id},
    ).mappings().all()

    today = date.today()
    due_soon_limit = today + timedelta(days=3)
    for row in rows:
        task_id = int(row["task_id"])
        tree_id = int(row["tree_id"])
        status = _normalize_name(row.get("status"))
        review_state = _normalize_name(row.get("review_state"))
        due_date = _parse_date_value(row.get("due_date"))
        evidence_ok, _ = _has_required_evidence(row.get("task_type"), row.get("notes"), row.get("photo_url"))

        if review_state == "submitted":
            _record_alert(
                db,
                project_id=project_id,
                alert_type="task_submitted",
                severity="warning",
                message=f"Task #{task_id} is awaiting supervisor review.",
                tree_id=tree_id,
                task_id=task_id,
                payload={"review_state": review_state},
            )

        if status != "done" and due_date:
            if due_date < today:
                days = abs(_day_diff(due_date, today))
                _record_alert(
                    db,
                    project_id=project_id,
                    alert_type="task_overdue",
                    severity="danger",
                    message=f"Task #{task_id} overdue by {days} day{'s' if days != 1 else ''}.",
                    tree_id=tree_id,
                    task_id=task_id,
                    payload={"due_date": _to_date_input(due_date)},
                )
            elif due_date <= due_soon_limit:
                left_days = max(_day_diff(due_date, today), 0)
                _record_alert(
                    db,
                    project_id=project_id,
                    alert_type="task_due_soon",
                    severity="warning",
                    message=f"Task #{task_id} due in {left_days} day{'s' if left_days != 1 else ''}.",
                    tree_id=tree_id,
                    task_id=task_id,
                    payload={"due_date": _to_date_input(due_date)},
                )

        if _is_done_status(status) and review_state == "submitted" and not evidence_ok:
            _record_alert(
                db,
                project_id=project_id,
                alert_type="missing_evidence",
                severity="danger",
                message=f"Task #{task_id} submitted without complete evidence.",
                tree_id=tree_id,
                task_id=task_id,
                payload={"task_type": row.get("task_type")},
            )


def _compute_live_maintenance_rows(
    db: Session,
    project_id: int,
    season_mode: str = "rainy",
    assignee_name: str | None = None,
) -> dict:
    season = "dry" if _normalize_name(season_mode) == "dry" else "rainy"
    assignee_key = _normalize_name(assignee_name)
    trees = db.execute(
        text("""
            SELECT id, created_by, status, species, planting_date
            FROM trees
            WHERE project_id = :project_id
            ORDER BY id ASC
        """),
        {"project_id": project_id},
    ).mappings().all()
    task_rows = db.execute(
        text("""
            SELECT t.id, t.tree_id, t.task_type, t.assignee_name, t.status, t.review_state, t.due_date,
                   t.priority, t.notes, t.photo_url, t.created_at, t.completed_at
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE tr.project_id = :project_id
              AND (:assignee_name IS NULL OR t.assignee_name = :assignee_name)
            ORDER BY t.created_at DESC
        """),
        {"project_id": project_id, "assignee_name": assignee_name},
    ).mappings().all()

    species_maturity_map = _get_species_maturity_map(project_id, db)
    task_buckets: dict[str, list[dict]] = {}
    for task in task_rows:
        activity = _normalize_name(task.get("task_type"))
        if activity not in MAINTENANCE_ACTIVITY_ORDER:
            continue
        key = f"{int(task['tree_id'])}:{activity}"
        task_buckets.setdefault(key, []).append(dict(task))

    today = date.today()
    rows: list[dict] = []
    for tree in trees:
        tree_id = int(tree["id"])
        tree_assignee = str(tree.get("created_by") or "-")
        if assignee_key:
            has_matching_task = any(
                _normalize_name(str(task.get("assignee_name") or "")) == assignee_key and int(task["tree_id"]) == tree_id
                for task in task_rows
            )
            if _normalize_name(tree_assignee) != assignee_key and not has_matching_task:
                continue

        tree_status = _normalize_tree_status(tree.get("status") or "alive")
        replacement_required = _is_replacement_trigger_status(tree_status)
        planting_date_obj = _parse_date_value(tree.get("planting_date"))
        replacement_key = f"{tree_id}:replacement"
        replacement_done = sorted(
            [
                _parse_date_value(task.get("completed_at") or task.get("due_date") or task.get("created_at"))
                for task in task_buckets.get(replacement_key, [])
                if _is_done_status(task.get("status")) and _normalize_name(task.get("review_state")) in {"approved", "none"}
            ],
            reverse=True,
        )
        latest_replacement_date = replacement_done[0] if replacement_done else None
        lifecycle_start = _get_lifecycle_start_date(planting_date_obj, latest_replacement_date)
        tree_age_days = _day_diff(today, lifecycle_start) if lifecycle_start else None
        species_key = _normalize_name(tree.get("species"))
        maturity_years = species_maturity_map.get(species_key) if species_key else None
        maturity_reached = (
            tree_status in HEALTHY_TREE_STATUSES
            and maturity_years is not None
            and tree_age_days is not None
            and tree_age_days >= maturity_years * 365
        )

        for activity in MAINTENANCE_ACTIVITY_ORDER:
            if activity == "replacement" and not replacement_required:
                # Replacement is condition-triggered only; hide it unless current tree status requires it.
                continue
            bucket = task_buckets.get(f"{tree_id}:{activity}", [])
            done_tasks = [
                task
                for task in bucket
                if _is_done_status(task.get("status")) and _normalize_name(task.get("review_state")) in {"approved", "none"}
            ]
            open_tasks = [task for task in bucket if not (_is_done_status(task.get("status")) and _normalize_name(task.get("review_state")) in {"approved", "none"})]
            done_tasks.sort(
                key=lambda task: _parse_date_value(task.get("completed_at") or task.get("due_date") or task.get("created_at")) or date.min,
                reverse=True,
            )
            open_tasks.sort(
                key=lambda task: _parse_date_value(task.get("due_date") or task.get("created_at")) or date.max,
            )
            latest_done = done_tasks[0] if done_tasks else None
            active_task = open_tasks[0] if open_tasks else None

            status_text = "No open task"
            tone = "ok"
            indicator = "On schedule"
            model_due: date | None = None
            assigned_due = _parse_date_value(active_task.get("due_date")) if active_task else None

            if replacement_required and activity != "replacement":
                tone = "danger"
                status_text = "Paused"
                indicator = f"Tree status '{tree_status.replace('_', ' ')}' requires replacement first."
            elif activity == "replacement" and replacement_required:
                model_due = today
                if active_task:
                    tone = "warning"
                    status_text = f"Task #{active_task['id']} {active_task.get('status') or 'pending'}"
                    indicator = "Replacement assigned."
                else:
                    tone = "danger"
                    status_text = "Replacement required"
                    indicator = "Replacement due immediately."
            elif tree_status == "need_watering" and activity == "watering":
                model_due = today
                tone = "warning"
                status_text = f"Task #{active_task['id']} {active_task.get('status') or 'pending'}" if active_task else "Action required"
                indicator = "Inspection flagged need watering. Due immediately."
            elif tree_status == "need_protection" and activity == "protection":
                model_due = today
                tone = "warning"
                status_text = f"Task #{active_task['id']} {active_task.get('status') or 'pending'}" if active_task else "Action required"
                indicator = "Inspection flagged need protection. Due immediately."
            elif maturity_reached:
                tone = "info"
                status_text = "Lifecycle complete"
                indicator = f"Tree reached maturity (~{maturity_years} years)."
            else:
                intervals = _get_maintenance_intervals(activity, tree_age_days or 0, season)
                latest_done_date = _parse_date_value(
                    latest_done.get("completed_at") if latest_done else None
                ) or _parse_date_value(latest_done.get("due_date") if latest_done else None) or _parse_date_value(
                    latest_done.get("created_at") if latest_done else None
                )
                if latest_done_date:
                    model_due = _add_days(latest_done_date, intervals["repeat_days"])
                elif lifecycle_start:
                    model_due = _add_days(lifecycle_start, intervals["first_days"])

                if active_task:
                    rs = _normalize_name(active_task.get("review_state"))
                    if rs == "submitted":
                        status_text = f"Task #{active_task['id']} submitted"
                    elif rs == "rejected":
                        status_text = f"Task #{active_task['id']} rejected"
                    else:
                        status_text = f"Task #{active_task['id']} {active_task.get('status') or 'pending'}"

            effective_due: date | None = None
            if model_due and assigned_due:
                effective_due = model_due if model_due <= assigned_due else assigned_due
            else:
                effective_due = model_due or assigned_due

            countdown_days = _day_diff(effective_due, today) if effective_due else None
            if countdown_days is not None:
                if countdown_days < 0:
                    tone = "danger"
                    indicator = f"Not done, overdue by {abs(countdown_days)} day{'s' if abs(countdown_days) != 1 else ''}."
                elif countdown_days == 0:
                    tone = "warning"
                    indicator = "Due today."
                elif countdown_days <= 7:
                    tone = "warning"
                    indicator = f"Due in {countdown_days} day{'s' if countdown_days != 1 else ''}."

            overdue_open = 0
            for task in open_tasks:
                due = _parse_date_value(task.get("due_date"))
                if due and due < today:
                    overdue_open += 1

            intervals = _get_maintenance_intervals(activity, tree_age_days or 0, season)
            if activity == "replacement":
                rationale = (
                    "Replacement is condition-triggered (dead/damaged/removed/needs replacement) "
                    "and is not treated as a routine cyclical task."
                )
            else:
                rationale = f"{season.title()} season model: first {intervals['first_days']}d, repeat {intervals['repeat_days']}d."
            rows.append(
                {
                    "key": f"{tree_id}:{activity}",
                    "treeId": tree_id,
                    "assignee": tree_assignee,
                    "activity": activity,
                    "activityLabel": activity.replace("_", " ").title(),
                    "plantingDate": _to_date_input(planting_date_obj),
                    "treeAgeDays": tree_age_days,
                    "lastDoneAt": _to_date_input(_parse_date_value(latest_done.get("completed_at") if latest_done else None) or _parse_date_value(latest_done.get("due_date") if latest_done else None)),
                    "modelDueDate": _to_date_input(model_due),
                    "assignedDueDate": _to_date_input(assigned_due),
                    "effectiveDueDate": _to_date_input(effective_due),
                    "countdownDays": countdown_days,
                    "tone": tone,
                    "indicator": indicator,
                    "statusText": status_text,
                    "doneCount": len(done_tasks),
                    "pendingCount": len(open_tasks),
                    "overdueCount": overdue_open,
                    "openTaskId": int(active_task["id"]) if active_task else None,
                    "modelRationale": rationale,
                }
            )

    tone_order = {"danger": 0, "warning": 1, "info": 2, "ok": 3}
    rows.sort(
        key=lambda item: (
            tone_order.get(item.get("tone"), 3),
            item.get("countdownDays") if item.get("countdownDays") is not None else 999999,
            int(item.get("treeId") or 0),
        )
    )

    summary = {
        "total": len(rows),
        "danger": sum(1 for item in rows if item.get("tone") == "danger"),
        "warning": sum(1 for item in rows if item.get("tone") == "warning"),
        "ok": sum(1 for item in rows if item.get("tone") == "ok"),
        "info": sum(1 for item in rows if item.get("tone") == "info"),
        "dueSoon": sum(1 for item in rows if isinstance(item.get("countdownDays"), int) and 0 <= item["countdownDays"] <= 7),
    }
    return {"rows": rows, "summary": summary}


def _auto_schedule_next_cycle(db: Session, task_id: int, season_hint: str | None = None) -> int | None:
    task = db.execute(
        text("""
            SELECT t.id, t.tree_id, t.task_type, t.assignee_name, t.priority, t.status, t.review_state,
                   t.completed_at, t.due_date, t.model_season,
                   tr.project_id, tr.status AS tree_status, tr.species, tr.planting_date
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE t.id = :task_id
        """),
        {"task_id": task_id},
    ).mappings().first()
    if not task:
        return None

    if _normalize_name(task.get("task_type")) not in MAINTENANCE_ACTIVITY_ORDER:
        return None
    if not _is_done_status(task.get("status")):
        return None
    if _normalize_name(task.get("review_state")) != "approved":
        return None

    tree_status = _normalize_tree_status(task.get("tree_status") or "alive")
    activity = _normalize_name(task.get("task_type"))
    if activity == "replacement":
        # Replacement is condition-triggered only; do not auto-generate recurring replacement cycles.
        return None
    if _is_replacement_trigger_status(tree_status):
        return None

    season = _normalize_name(season_hint or task.get("model_season") or "rainy")
    if season not in SEASON_VALUES:
        season = "rainy"

    project_id = int(task["project_id"])
    tree_id = int(task["tree_id"])
    today = date.today()
    completed_date = _parse_date_value(task.get("completed_at")) or _parse_date_value(task.get("due_date")) or today

    replacement_done = db.execute(
        text("""
            SELECT COALESCE(completed_at::date, due_date, created_at::date) AS stamp
            FROM tree_tasks
            WHERE tree_id = :tree_id
              AND LOWER(task_type) = 'replacement'
              AND LOWER(status) IN ('done', 'completed', 'closed')
              AND LOWER(review_state) = 'approved'
            ORDER BY stamp DESC
            LIMIT 1
        """),
        {"tree_id": tree_id},
    ).scalar()
    lifecycle_start = _get_lifecycle_start_date(
        _parse_date_value(task.get("planting_date")),
        _parse_date_value(replacement_done),
    )
    tree_age_days = _day_diff(today, lifecycle_start) if lifecycle_start else 0
    species_key = _normalize_name(task.get("species"))
    maturity_map = _get_species_maturity_map(project_id, db)
    maturity_years = maturity_map.get(species_key) if species_key else None
    if tree_status in HEALTHY_TREE_STATUSES and maturity_years and tree_age_days >= maturity_years * 365:
        return None

    open_exists = db.execute(
        text("""
            SELECT id
            FROM tree_tasks
            WHERE tree_id = :tree_id
              AND LOWER(task_type) = :task_type
              AND (
                LOWER(status) NOT IN ('done', 'completed', 'closed')
                OR LOWER(review_state) IN ('submitted', 'rejected', 'reopened')
              )
            LIMIT 1
        """),
        {"tree_id": tree_id, "task_type": activity},
    ).scalar()
    if open_exists:
        return None

    intervals = _get_maintenance_intervals(activity, tree_age_days, season)
    next_due = _add_days(completed_date, intervals["repeat_days"])
    new_task_id = db.execute(
        text("""
            INSERT INTO tree_tasks (
                tree_id, task_type, assignee_name, due_date, priority, status, notes,
                auto_generated, model_season, source_task_id, review_state
            )
            VALUES (
                :tree_id, :task_type, :assignee_name, :due_date, :priority, 'pending', :notes,
                TRUE, :model_season, :source_task_id, 'none'
            )
            RETURNING id
        """),
        {
            "tree_id": tree_id,
            "task_type": activity,
            "assignee_name": task.get("assignee_name"),
            "due_date": next_due,
            "priority": task.get("priority") or "normal",
            "notes": f"Auto-generated next cycle from Task #{task_id}.",
            "model_season": season,
            "source_task_id": task_id,
        },
    ).scalar()

    db.execute(
        text("""
            INSERT INTO green_maintenance_cycles (
                project_id, tree_id, task_type, model_season, source_task_id, generated_task_id, due_date, status
            )
            VALUES (
                :project_id, :tree_id, :task_type, :model_season, :source_task_id, :generated_task_id, :due_date, 'scheduled'
            )
        """),
        {
            "project_id": project_id,
            "tree_id": tree_id,
            "task_type": activity,
            "model_season": season,
            "source_task_id": task_id,
            "generated_task_id": new_task_id,
            "due_date": next_due,
        },
    )
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="task",
        entity_id=int(new_task_id),
        action="auto_cycle_generated",
        actor="system",
        details={
            "source_task_id": task_id,
            "season": season,
            "due_date": _to_date_input(next_due),
        },
    )
    return int(new_task_id)


def ensure_green_tables(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tree_projects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            location_text TEXT,
            sponsor TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS trees (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            geom GEOMETRY(POINT, 4326),
            species TEXT,
            planting_date DATE,
            status TEXT NOT NULL DEFAULT 'alive',
            notes TEXT,
            photo_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tree_visits (
            id SERIAL PRIMARY KEY,
            tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
            visit_date DATE NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            photo_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_users (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'field_officer',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tree_tasks (
            id SERIAL PRIMARY KEY,
            tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            assignee_name TEXT NOT NULL,
            due_date DATE,
            priority TEXT DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'pending',
            notes TEXT,
            photo_url TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP
        )
    """))
    try:
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'normal'"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS review_state TEXT NOT NULL DEFAULT 'none'"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS reviewed_by TEXT"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS review_notes TEXT"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS auto_generated BOOLEAN NOT NULL DEFAULT FALSE"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS model_season TEXT"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS source_task_id INTEGER"))
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS reported_tree_status TEXT"))
    except Exception:
        db.rollback()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_work_orders (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            assignee_name TEXT NOT NULL,
            work_type TEXT NOT NULL,
            target_trees INTEGER DEFAULT 0,
            maintenance_schedule TEXT,
            due_date DATE,
            status TEXT NOT NULL DEFAULT 'assigned',
            planted_count INTEGER DEFAULT 0,
            visits_done INTEGER DEFAULT 0,
            last_update TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_species_maturity (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            species_key TEXT NOT NULL,
            species_label TEXT,
            maturity_years INTEGER NOT NULL CHECK (maturity_years > 0),
            updated_at TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(project_id, species_key)
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_task_reviews (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tree_tasks(id) ON DELETE CASCADE,
            decision TEXT NOT NULL,
            reviewer_name TEXT,
            review_notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_audit_events (
            id SERIAL PRIMARY KEY,
            project_id INTEGER REFERENCES tree_projects(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            actor TEXT,
            details JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_maintenance_cycles (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            model_season TEXT,
            source_task_id INTEGER REFERENCES tree_tasks(id) ON DELETE SET NULL,
            generated_task_id INTEGER REFERENCES tree_tasks(id) ON DELETE SET NULL,
            due_date DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_alerts (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            tree_id INTEGER REFERENCES trees(id) ON DELETE SET NULL,
            task_id INTEGER REFERENCES tree_tasks(id) ON DELETE SET NULL,
            alert_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warning',
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            payload JSONB,
            created_at TIMESTAMP DEFAULT NOW(),
            resolved_at TIMESTAMP
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_scheduled_reports (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            report_type TEXT NOT NULL DEFAULT 'donor',
            report_format TEXT NOT NULL DEFAULT 'pdf',
            recipients TEXT NOT NULL DEFAULT '',
            cron_expr TEXT,
            timezone TEXT NOT NULL DEFAULT 'Africa/Lagos',
            webhook_url TEXT,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by TEXT,
            last_run_at TIMESTAMP,
            next_run_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_kpi_snapshots (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            snapshot_at TIMESTAMP NOT NULL DEFAULT NOW(),
            metrics JSONB NOT NULL
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_alert_rules (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            rule_name TEXT NOT NULL,
            metric_key TEXT NOT NULL,
            comparator TEXT NOT NULL DEFAULT 'gte',
            threshold NUMERIC NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warning',
            message_template TEXT,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_alert_events (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            rule_id INTEGER REFERENCES green_alert_rules(id) ON DELETE SET NULL,
            severity TEXT NOT NULL DEFAULT 'warning',
            status TEXT NOT NULL DEFAULT 'open',
            metric_key TEXT,
            metric_value NUMERIC,
            threshold NUMERIC,
            message TEXT NOT NULL,
            payload JSONB,
            triggered_at TIMESTAMP DEFAULT NOW(),
            resolved_at TIMESTAMP
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_webhook_deliveries (
            id SERIAL PRIMARY KEY,
            event_id INTEGER REFERENCES green_alert_events(id) ON DELETE CASCADE,
            target_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            response_code INTEGER,
            response_body TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            delivered_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_trees_project_id ON trees(project_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_trees_geom ON trees USING GIST (geom)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_tree_visits_tree_id ON tree_visits(tree_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_tree_tasks_tree_id ON tree_tasks(tree_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_tree_tasks_review_state ON tree_tasks(review_state)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_green_users_name ON green_users(full_name)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_work_orders_project_id ON green_work_orders(project_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_species_maturity_project_id ON green_species_maturity(project_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_task_reviews_task_id ON green_task_reviews(task_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_project_created ON green_audit_events(project_id, created_at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_cycles_project_due ON green_maintenance_cycles(project_id, due_date)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_alerts_project_status ON green_alerts(project_id, status, created_at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_sched_reports_project ON green_scheduled_reports(project_id, is_enabled)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_kpi_snapshots_project_time ON green_kpi_snapshots(project_id, snapshot_at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_rules_project_enabled ON green_alert_rules(project_id, is_enabled)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_events_project_time ON green_alert_events(project_id, triggered_at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_webhook_event ON green_webhook_deliveries(event_id, created_at DESC)"))
    db.commit()


def _load_env_token() -> str | None:
    token_keys = ("MAPBOX_TOKEN", "MAPBOX_ACCESS_TOKEN", "MAPBOX_PUBLIC_TOKEN", "VITE_MAPBOX_TOKEN")
    for key in token_keys:
        token = os.environ.get(key)
        if token:
            return token.strip().strip('"').strip("'")
    env_path = os.path.join(BASE_DIR, "..", ".env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("#") or "=" not in line:
                    continue
                key, val = line.strip().split("=", 1)
                if key in {"MAPBOX_TOKEN", "MAPBOX_ACCESS_TOKEN", "MAPBOX_PUBLIC_TOKEN", "VITE_MAPBOX_TOKEN"}:
                    return val.strip().strip('"').strip("'")
    except Exception:
        return None
    return None


def _http_get_binary(url: str, timeout: int = 15) -> bytes | None:
    try:
        import requests

        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass

    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "LandCheck/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if data:
                return data
    except Exception:
        return None
    return None


def _build_report_map_png(
    map_rows: list[dict],
    lng: float | None = None,
    lat: float | None = None,
    zoom: float | None = None,
    bearing: float | None = 0.0,
    pitch: float | None = 0.0,
) -> bytes | None:
    if not map_rows:
        return None

    def _coerce_optional_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            if isinstance(value, bool):
                return None
            return float(value)
        except Exception:
            return None

    lng_value = _coerce_optional_float(lng)
    lat_value = _coerce_optional_float(lat)
    zoom_value = _coerce_optional_float(zoom)
    bearing_value = _coerce_optional_float(bearing)
    pitch_value = _coerce_optional_float(pitch)

    lats = [r.get("lat") for r in map_rows if r.get("lat") is not None]
    lngs = [r.get("lng") for r in map_rows if r.get("lng") is not None]
    if not lats or not lngs:
        return None

    center_lat = lat_value if lat_value is not None else sum(lats) / len(lats)
    center_lng = lng_value if lng_value is not None else sum(lngs) / len(lngs)
    z = zoom_value if zoom_value is not None else 13
    b = bearing_value if bearing_value is not None else 0
    p = pitch_value if pitch_value is not None else 0

    token = _load_env_token()
    if token:
        markers = []
        for r in map_rows[:60]:
            if r.get("lng") is None or r.get("lat") is None:
                continue
            color = _tree_status_color_hex(r.get("status"))
            markers.append(f"pin-s+{color}({r['lng']},{r['lat']})")
        overlay = ",".join(markers) if markers else ""
        overlay_part = f"{quote(overlay, safe='(),:+')}/" if overlay else ""
        for style in ("mapbox/satellite-streets-v12", "mapbox/satellite-v9"):
            if overlay:
                url = (
                    f"https://api.mapbox.com/styles/v1/{style}/static/"
                    f"{overlay_part}{center_lng},{center_lat},{z},{b},{p}/800x500@2x?access_token={token}"
                )
                map_png = _http_get_binary(url)
                if map_png:
                    return map_png
            url = (
                f"https://api.mapbox.com/styles/v1/{style}/static/"
                f"{center_lng},{center_lat},{z},{b},{p}/800x500@2x?access_token={token}"
            )
            map_png = _http_get_binary(url)
            if map_png:
                return map_png

    markers = "|".join([f"{r['lat']},{r['lng']},lightgreen1" for r in map_rows[:50] if r.get("lat") is not None and r.get("lng") is not None])
    marker_qs = quote(markers, safe="|,")
    osm_url = (
        "https://staticmap.openstreetmap.de/staticmap.php?"
        f"center={center_lat},{center_lng}&zoom={int(round(z))}&size=800x500&markers={marker_qs}"
    )
    return _http_get_binary(osm_url)


def _build_r2_settings() -> dict:
    default_endpoint = "https://751ea1abdb3fb6ff7f276b3753e4c6a1.r2.cloudflarestorage.com"
    default_bucket = "photosgreen"
    default_public_base = f"{default_endpoint}/{default_bucket}"

    endpoint_raw = (os.getenv("R2_ENDPOINT_URL") or default_endpoint).strip()
    public_base = (os.getenv("R2_PUBLIC_BASE_URL") or default_public_base).strip()
    bucket = (os.getenv("R2_BUCKET") or default_bucket).strip()
    access_key = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    region = (os.getenv("R2_REGION") or "auto").strip()

    raw_for_parse = endpoint_raw or public_base
    if not raw_for_parse:
        raise HTTPException(
            status_code=500,
            detail="R2 is not configured. Set R2_ENDPOINT_URL (or R2_PUBLIC_BASE_URL), R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.",
        )

    parsed = urlparse(raw_for_parse)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=500, detail="Invalid R2 endpoint/public base URL format.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and not bucket:
        bucket = path_parts[0]

    endpoint_url = endpoint_raw.strip() if endpoint_raw else f"{parsed.scheme}://{parsed.netloc}"
    endpoint_parsed = urlparse(endpoint_url)
    endpoint_url = f"{endpoint_parsed.scheme}://{endpoint_parsed.netloc}"

    if not public_base:
        public_base = f"{endpoint_url.rstrip('/')}/{bucket}"

    if not bucket:
        raise HTTPException(status_code=500, detail="R2 bucket is not configured. Set R2_BUCKET.")
    if not access_key or not secret_key:
        raise HTTPException(
            status_code=500,
            detail="R2 credentials are not configured. Set R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY.",
        )

    return {
        "endpoint_url": endpoint_url,
        "public_base": public_base.rstrip("/"),
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
    }


def _make_r2_client(settings: dict):
    return boto3.client(
        "s3",
        endpoint_url=settings["endpoint_url"],
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
    )


def _normalize_object_key(raw_key: str, bucket: str) -> str:
    key = (raw_key or "").strip().lstrip("/")
    if not key:
        return ""

    # Handle keys that were encoded once/twice by older clients.
    for _ in range(3):
        decoded = unquote(key)
        if decoded == key:
            break
        key = decoded

    bucket_prefix = f"{bucket}/"
    if key.startswith(bucket_prefix):
        key = key[len(bucket_prefix):]
    return key


@router.get("/uploads/object/{object_key:path}")
def get_uploaded_photo(object_key: str):
    settings = _build_r2_settings()
    resolved_key = _normalize_object_key(object_key, settings["bucket"])
    if not resolved_key:
        raise HTTPException(status_code=400, detail="Invalid photo key.")
    try:
        client = _make_r2_client(settings)
        obj = client.get_object(Bucket=settings["bucket"], Key=resolved_key)
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status_code=404, detail="Photo not found.")
        raise HTTPException(status_code=502, detail="Failed to read photo from storage.")
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to read photo from storage.")

    content_type = obj.get("ContentType") or "application/octet-stream"
    cache_control = obj.get("CacheControl") or "public, max-age=86400"
    return StreamingResponse(
        obj["Body"].iter_chunks(),
        media_type=content_type,
        headers={"Cache-Control": cache_control},
    )


@router.post("/uploads/photo")
async def upload_photo_to_r2(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    folder: str = Form(default="trees"),
    tree_id: int | None = Form(default=None),
    task_id: int | None = Form(default=None),
):
    if tree_id is not None and task_id is not None:
        raise HTTPException(status_code=400, detail="Provide either tree_id or task_id, not both.")

    content_type = (file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    max_bytes = 10 * 1024 * 1024
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="Image too large. Max size is 10MB.")

    settings = _build_r2_settings()

    ext = Path(file.filename or "").suffix.lower()
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}
    if ext not in allowed_ext:
        content_ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/heic": ".heic",
            "image/heif": ".heif",
        }
        ext = content_ext.get(content_type, ".jpg")

    folder_parts = [part for part in (folder or "trees").split("/") if part and part not in {".", ".."}]
    safe_folder = "/".join(folder_parts) or "trees"
    date_path = datetime.utcnow().strftime("%Y/%m")
    object_key = f"{safe_folder}/{date_path}/{uuid.uuid4().hex}{ext}"

    try:
        client = _make_r2_client(settings)
        client.put_object(
            Bucket=settings["bucket"],
            Key=object_key,
            Body=payload,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Photo upload failed.")

    public_url = f"{settings['public_base']}/{quote(object_key, safe='/')}"
    app_base = str(request.base_url).rstrip("/")
    proxy_url = f"{app_base}/green/uploads/object/{quote(object_key, safe='/')}"

    linked_tree_id = None
    linked_task_id = None

    if tree_id is not None:
        linked_tree_id = db.execute(text("""
            UPDATE trees
            SET photo_url = :photo_url
            WHERE id = :tree_id
            RETURNING id
        """), {"photo_url": proxy_url, "tree_id": tree_id}).scalar()
        if not linked_tree_id:
            db.rollback()
            raise HTTPException(status_code=404, detail="Tree not found for photo link.")
        db.commit()
    elif task_id is not None:
        locked = db.execute(
            text("""
                SELECT review_state
                FROM tree_tasks
                WHERE id = :task_id
            """),
            {"task_id": task_id},
        ).scalar()
        if locked and _normalize_name(str(locked)) == "approved":
            raise HTTPException(status_code=409, detail="Task already approved and locked")
        task_row = db.execute(text("""
            UPDATE tree_tasks
            SET photo_url = :photo_url
            WHERE id = :task_id
            RETURNING id, tree_id
        """), {"photo_url": proxy_url, "task_id": task_id}).mappings().first()
        if not task_row:
            db.rollback()
            raise HTTPException(status_code=404, detail="Task not found for photo link.")
        linked_task_id = task_row["id"]
        linked_tree_id = task_row["tree_id"]
        db.execute(text("""
            UPDATE trees
            SET photo_url = :photo_url
            WHERE id = :tree_id
        """), {"photo_url": proxy_url, "tree_id": linked_tree_id})
        db.commit()

    return {
        "url": proxy_url,
        "key": object_key,
        "public_url": public_url,
        "linked_tree_id": linked_tree_id,
        "linked_task_id": linked_task_id,
    }


@router.post("/projects")
def create_project(
    db: Session = Depends(get_db),
    name: str = Body(...),
    location_text: str = Body(default=""),
    sponsor: str = Body(default=""),
):
    row = db.execute(
        text("""
            INSERT INTO tree_projects (name, location_text, sponsor)
            VALUES (:name, :location_text, :sponsor)
            RETURNING id, name, location_text, sponsor, created_at
        """),
        {"name": name, "location_text": location_text, "sponsor": sponsor},
    ).mappings().first()
    project = dict(row)
    _log_audit_event(
        db,
        project_id=project["id"],
        entity_type="project",
        entity_id=project["id"],
        action="project_created",
        details={"name": project.get("name"), "location_text": project.get("location_text")},
    )
    db.commit()
    return project


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, name, location_text, sponsor, created_at
        FROM tree_projects
        ORDER BY created_at DESC
    """)).mappings().all()
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.execute(text("""
        SELECT id, name, location_text, sponsor, created_at
        FROM tree_projects
        WHERE id = :project_id
    """), {"project_id": project_id}).mappings().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    stats_rows = db.execute(
        text("""
            SELECT status, COUNT(*) AS count
            FROM trees
            WHERE project_id = :project_id
            GROUP BY status
        """),
        {"project_id": project_id},
    ).mappings().all()
    status_counts: dict[str, int] = {}
    for row in stats_rows:
        status_key = _normalize_tree_status(row.get("status"))
        status_counts[status_key] = status_counts.get(status_key, 0) + int(row.get("count") or 0)

    total = sum(status_counts.values())
    alive = sum(status_counts.get(status_key, 0) for status_key in HEALTHY_TREE_STATUSES)
    dead = sum(status_counts.get(status_key, 0) for status_key in DEAD_TREE_STATUSES)
    needs_attention = sum(status_counts.get(status_key, 0) for status_key in ATTENTION_TREE_STATUSES)
    survival_rate = round((alive / total) * 100, 1) if total else 0.0

    return {
        **dict(project),
        "stats": {
            "total": total,
            "alive": alive,
            "dead": dead,
            "needs_attention": needs_attention,
            "survival_rate": survival_rate,
        },
    }


@router.get("/projects/{project_id}/trees")
def list_trees(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, project_id, species, planting_date, status, notes, photo_url, created_by, created_at,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
    """), {"project_id": project_id}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}/species-maturity")
def get_species_maturity(project_id: int, db: Session = Depends(get_db)):
    project_exists = db.execute(
        text("SELECT 1 FROM tree_projects WHERE id = :project_id"),
        {"project_id": project_id},
    ).scalar()
    if not project_exists:
        raise HTTPException(status_code=404, detail="Project not found")

    rows = db.execute(
        text("""
            SELECT species_key, species_label, maturity_years, updated_at
            FROM green_species_maturity
            WHERE project_id = :project_id
            ORDER BY COALESCE(species_label, species_key) ASC
        """),
        {"project_id": project_id},
    ).mappings().all()
    items = [dict(row) for row in rows]
    return {
        "project_id": project_id,
        "items": items,
        "map": {row["species_key"]: int(row["maturity_years"]) for row in rows},
    }


@router.put("/projects/{project_id}/species-maturity")
def upsert_species_maturity(
    project_id: int,
    db: Session = Depends(get_db),
    species_key: str = Body(...),
    maturity_years: int = Body(...),
    species_label: str | None = Body(default=None),
):
    normalized_key = (species_key or "").strip().lower()
    if not normalized_key:
        raise HTTPException(status_code=400, detail="species_key is required")
    if maturity_years < 1 or maturity_years > 50:
        raise HTTPException(status_code=400, detail="maturity_years must be between 1 and 50")

    project_exists = db.execute(
        text("SELECT 1 FROM tree_projects WHERE id = :project_id"),
        {"project_id": project_id},
    ).scalar()
    if not project_exists:
        raise HTTPException(status_code=404, detail="Project not found")

    cleaned_label = (species_label or "").strip() or None
    row = db.execute(
        text("""
            INSERT INTO green_species_maturity (project_id, species_key, species_label, maturity_years)
            VALUES (:project_id, :species_key, :species_label, :maturity_years)
            ON CONFLICT (project_id, species_key)
            DO UPDATE
            SET
                maturity_years = EXCLUDED.maturity_years,
                species_label = COALESCE(EXCLUDED.species_label, green_species_maturity.species_label),
                updated_at = NOW()
            RETURNING project_id, species_key, species_label, maturity_years, updated_at
        """),
        {
            "project_id": project_id,
            "species_key": normalized_key,
            "species_label": cleaned_label,
            "maturity_years": int(maturity_years),
        },
    ).mappings().first()
    db.commit()
    return dict(row)


@router.post("/trees")
def add_tree(
    db: Session = Depends(get_db),
    project_id: int = Body(...),
    lng: float = Body(...),
    lat: float = Body(...),
    species: str = Body(default=""),
    planting_date: str | None = Body(default=None),
    status: str = Body(default="alive"),
    notes: str = Body(default=""),
    photo_url: str = Body(default=""),
    created_by: str = Body(default=""),
):
    normalized_status = _normalize_tree_status(status or "alive")
    if normalized_status not in TREE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="Invalid status")
    row = db.execute(text("""
        INSERT INTO trees (project_id, geom, species, planting_date, status, notes, photo_url, created_by)
        VALUES (
            :project_id,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            :species,
            :planting_date,
            :status,
            :notes,
            :photo_url,
            :created_by
        )
        RETURNING id
    """), {
        "project_id": project_id,
        "lng": lng,
        "lat": lat,
        "species": species or None,
        "planting_date": planting_date,
        "status": normalized_status,
        "notes": notes or None,
        "photo_url": photo_url or None,
        "created_by": created_by or None,
    }).scalar()
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="tree",
        entity_id=int(row),
        action="tree_created",
        actor=created_by or None,
        details={
            "species": species or None,
            "status": normalized_status,
            "planting_date": planting_date,
            "lng": lng,
            "lat": lat,
        },
    )
    db.commit()
    return {"id": row}


@router.patch("/trees/{tree_id}")
def update_tree(
    tree_id: int,
    db: Session = Depends(get_db),
    species: str | None = Body(default=None),
    planting_date: str | None = Body(default=None),
    status: str | None = Body(default=None),
    notes: str | None = Body(default=None),
    photo_url: str | None = Body(default=None),
):
    normalized_status = _normalize_tree_status(status) if status is not None else None
    if normalized_status is not None and normalized_status not in TREE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="Invalid status")
    existing = db.execute(
        text("SELECT project_id, species, planting_date, status, notes, photo_url FROM trees WHERE id = :tree_id"),
        {"tree_id": tree_id},
    ).mappings().first()
    if not existing:
        raise HTTPException(status_code=404, detail="Tree not found")

    db.execute(text("""
        UPDATE trees
        SET species = COALESCE(:species, species),
            planting_date = COALESCE(:planting_date, planting_date),
            status = COALESCE(:status, status),
            notes = COALESCE(:notes, notes),
            photo_url = COALESCE(:photo_url, photo_url)
        WHERE id = :tree_id
    """), {
        "species": species,
        "planting_date": planting_date,
        "status": normalized_status,
        "notes": notes,
        "photo_url": photo_url,
        "tree_id": tree_id,
    })
    _log_audit_event(
        db,
        project_id=int(existing["project_id"]),
        entity_type="tree",
        entity_id=tree_id,
        action="tree_updated",
        details={
            "before": dict(existing),
            "after": {
                "species": species if species is not None else existing.get("species"),
                "planting_date": planting_date if planting_date is not None else existing.get("planting_date"),
                "status": normalized_status if normalized_status is not None else existing.get("status"),
                "notes": notes if notes is not None else existing.get("notes"),
                "photo_url": photo_url if photo_url is not None else existing.get("photo_url"),
            },
        },
    )
    db.commit()
    return {"status": "ok"}


@router.post("/trees/{tree_id}/visits")
def add_visit(
    tree_id: int,
    db: Session = Depends(get_db),
    visit_date: str = Body(...),
    status: str = Body(...),
    notes: str = Body(default=""),
    photo_url: str = Body(default=""),
    created_by: str = Body(default=""),
):
    normalized_status = _normalize_tree_status(status)
    if normalized_status not in TREE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="Invalid status")
    db.execute(text("""
        INSERT INTO tree_visits (tree_id, visit_date, status, notes, photo_url, created_by)
        VALUES (:tree_id, :visit_date, :status, :notes, :photo_url, :created_by)
    """), {
        "tree_id": tree_id,
        "visit_date": visit_date,
        "status": normalized_status,
        "notes": notes or None,
        "photo_url": photo_url or None,
        "created_by": created_by or None,
    })
    db.commit()
    return {"status": "ok"}


@router.post("/trees/{tree_id}/tasks")
def add_task(
    tree_id: int,
    db: Session = Depends(get_db),
    task_type: str = Body(...),
    assignee_name: str = Body(...),
    due_date: str | None = Body(default=None),
    priority: str = Body(default="normal"),
    status: str = Body(default="pending"),
    notes: str = Body(default=""),
    photo_url: str = Body(default=""),
    model_season: str | None = Body(default=None),
):
    if status not in {"pending", "done", "overdue"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    activity = _normalize_name(task_type)
    if activity not in MAINTENANCE_ACTIVITY_ORDER:
        raise HTTPException(status_code=400, detail="Invalid maintenance type")
    tree_row = db.execute(
        text("SELECT project_id, status FROM trees WHERE id = :tree_id"),
        {"tree_id": tree_id},
    ).mappings().first()
    if not tree_row:
        raise HTTPException(status_code=404, detail="Tree not found")
    tree_status = _normalize_tree_status(tree_row.get("status") or "alive")
    replacement_required = _is_replacement_trigger_status(tree_status)
    if activity == "replacement" and not replacement_required:
        raise HTTPException(
            status_code=400,
            detail="Replacement can only be assigned when tree status is dead, damaged, removed, or needs replacement.",
        )
    if replacement_required and activity != "replacement":
        raise HTTPException(
            status_code=400,
            detail="This tree currently requires replacement. Assign and complete replacement first.",
        )

    normalized_season = _normalize_name(model_season)
    if normalized_season and normalized_season not in SEASON_VALUES:
        normalized_season = "rainy"

    review_state = "none"
    submitted_at = None
    completed_at = None
    if _is_done_status(status):
        evidence_ok, detail = _has_required_evidence(activity, notes, photo_url)
        if not evidence_ok:
            raise HTTPException(status_code=400, detail=detail)
        review_state = "submitted"
        submitted_at = datetime.utcnow()
        completed_at = datetime.utcnow()

    row = db.execute(text("""
        INSERT INTO tree_tasks (
            tree_id, task_type, assignee_name, due_date, priority, status, notes, photo_url,
            review_state, submitted_at, completed_at, model_season
        )
        VALUES (
            :tree_id, :task_type, :assignee_name, :due_date, :priority, :status, :notes, :photo_url,
            :review_state, :submitted_at, :completed_at, :model_season
        )
        RETURNING id
    """), {
        "tree_id": tree_id,
        "task_type": activity,
        "assignee_name": assignee_name,
        "due_date": due_date,
        "priority": priority,
        "status": status,
        "notes": notes or None,
        "photo_url": photo_url or None,
        "review_state": review_state,
        "submitted_at": submitted_at,
        "completed_at": completed_at,
        "model_season": normalized_season or None,
    }).scalar()
    _log_audit_event(
        db,
        project_id=int(tree_row["project_id"]),
        entity_type="task",
        entity_id=int(row),
        action="task_created",
        actor=assignee_name,
        details={
            "task_type": activity,
            "due_date": due_date,
            "priority": priority,
            "status": status,
            "review_state": review_state,
            "model_season": normalized_season or None,
        },
    )
    if review_state == "submitted":
        _record_alert(
            db,
            project_id=int(tree_row["project_id"]),
            alert_type="task_submitted",
            severity="warning",
            message=f"Task #{int(row)} is awaiting supervisor review.",
            tree_id=tree_id,
            task_id=int(row),
        )
    db.commit()
    return {"id": row}


@router.get("/trees/{tree_id}/tasks")
def list_tree_tasks(tree_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, tree_id, task_type, assignee_name, due_date, priority,
               status, notes, photo_url, created_at, completed_at, review_state,
               submitted_at, reviewed_at, reviewed_by, review_notes, auto_generated, model_season, source_task_id,
               reported_tree_status
        FROM tree_tasks
        WHERE tree_id = :tree_id
        ORDER BY created_at DESC
    """), {"tree_id": tree_id}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/tasks")
def list_tasks(
    project_id: int,
    assignee_name: str | None = None,
    db: Session = Depends(get_db),
):
    rows = db.execute(text("""
        SELECT t.id, t.tree_id, t.task_type, t.assignee_name, t.due_date, t.priority,
               t.status, t.notes, t.photo_url, t.created_at, t.completed_at, t.review_state,
               t.submitted_at, t.reviewed_at, t.reviewed_by, t.review_notes, t.auto_generated, t.model_season, t.source_task_id,
               t.reported_tree_status,
               tr.status AS tree_status, ST_X(tr.geom) AS lng, ST_Y(tr.geom) AS lat
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE tr.project_id = :project_id
          AND (:assignee_name IS NULL OR t.assignee_name = :assignee_name)
        ORDER BY t.created_at DESC
    """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
    return [dict(r) for r in rows]


@router.post("/users")
def create_user(
    db: Session = Depends(get_db),
    full_name: str = Body(...),
    role: str = Body(default="field_officer"),
):
    row = db.execute(text("""
        INSERT INTO green_users (full_name, role)
        VALUES (:full_name, :role)
        RETURNING id, full_name, role, created_at
    """), {"full_name": full_name, "role": role}).mappings().first()
    db.commit()
    return dict(row)


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, full_name, role, created_at
        FROM green_users
        ORDER BY created_at DESC
    """)).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/tasks/{task_id}")
def update_task(
    task_id: int,
    db: Session = Depends(get_db),
    status: str | None = Body(default=None),
    notes: str | None = Body(default=None),
    photo_url: str | None = Body(default=None),
    tree_status: str | None = Body(default=None),
    actor_name: str | None = Body(default=None),
):
    if status and status not in TASK_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="Invalid status")
    normalized_tree_status = _normalize_tree_status(tree_status) if tree_status is not None else None
    if normalized_tree_status is not None and normalized_tree_status not in TREE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="Invalid tree status")
    existing = db.execute(text("""
        SELECT t.id, t.tree_id, t.task_type, t.status, t.review_state, t.notes, t.photo_url,
               t.completed_at,
               t.submitted_at, t.reviewed_at, t.reviewed_by, t.review_notes, t.reported_tree_status,
               tr.project_id, tr.status AS tree_status
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE t.id = :task_id
    """), {"task_id": task_id}).mappings().first()
    if not existing:
        raise HTTPException(status_code=404, detail="Task not found")
    if _normalize_name(existing.get("review_state")) == "approved":
        raise HTTPException(status_code=409, detail="Task already approved and locked")
    next_status = status or existing.get("status")
    next_notes = notes if notes is not None else existing.get("notes")
    next_photo = photo_url if photo_url is not None else existing.get("photo_url")
    next_review_state = existing.get("review_state") or "none"
    next_submitted_at = existing.get("submitted_at")
    next_completed_at = existing.get("completed_at")

    clear_review_fields = False
    if _is_done_status(next_status):
        evidence_ok, detail = _has_required_evidence(existing.get("task_type"), next_notes, next_photo)
        if not evidence_ok:
            raise HTTPException(status_code=400, detail=detail)
        next_review_state = "submitted"
        next_submitted_at = datetime.utcnow()
        next_completed_at = datetime.utcnow()
        clear_review_fields = True
    elif status is not None:
        # Task moved out of done-state; keep it editable.
        next_completed_at = None
        current_review_state = _normalize_name(existing.get("review_state"))
        if current_review_state == "rejected":
            # Keep rejected state visible until staff explicitly resubmits.
            next_review_state = "rejected"
            clear_review_fields = False
        else:
            if _normalize_name(next_review_state) in {"submitted", "rejected", "reopened"}:
                next_review_state = "none"
            next_submitted_at = None
            clear_review_fields = True

    row = db.execute(text("""
        UPDATE tree_tasks
        SET status = COALESCE(:status, status),
            notes = COALESCE(:notes, notes),
            photo_url = COALESCE(:photo_url, photo_url),
            reported_tree_status = COALESCE(:reported_tree_status, reported_tree_status),
            review_state = :review_state,
            submitted_at = :submitted_at,
            reviewed_at = CASE WHEN :clear_review_fields THEN NULL ELSE reviewed_at END,
            reviewed_by = CASE WHEN :clear_review_fields THEN NULL ELSE reviewed_by END,
            review_notes = CASE WHEN :clear_review_fields THEN NULL ELSE review_notes END,
            completed_at = :completed_at
        WHERE id = :task_id
        RETURNING tree_id, photo_url, status, review_state, reported_tree_status
    """), {
        "status": status,
        "notes": notes,
        "photo_url": photo_url,
        "reported_tree_status": normalized_tree_status,
        "review_state": next_review_state,
        "submitted_at": next_submitted_at,
        "clear_review_fields": clear_review_fields,
        "completed_at": next_completed_at,
        "task_id": task_id,
    }).mappings().first()
    resolved_photo = row.get("photo_url")
    if resolved_photo:
        db.execute(text("""
            UPDATE trees
            SET photo_url = COALESCE(:photo_url, photo_url)
            WHERE id = :tree_id
        """), {"photo_url": resolved_photo, "tree_id": row["tree_id"]})

    project_id = int(existing["project_id"])
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="task",
        entity_id=task_id,
        action="task_updated",
        actor=actor_name,
        details={
            "before": {
                "status": existing.get("status"),
                "review_state": existing.get("review_state"),
                "notes": existing.get("notes"),
                "photo_url": existing.get("photo_url"),
                "tree_status": existing.get("tree_status"),
                "reported_tree_status": existing.get("reported_tree_status"),
            },
            "after": {
                "status": row.get("status"),
                "review_state": row.get("review_state"),
                "notes": next_notes,
                "photo_url": next_photo,
                "tree_status": existing.get("tree_status"),
                "reported_tree_status": row.get("reported_tree_status"),
            },
        },
    )
    if _normalize_name(row.get("review_state")) == "submitted":
        _record_alert(
            db,
            project_id=project_id,
            alert_type="task_submitted",
            severity="warning",
            message=f"Task #{task_id} is awaiting supervisor review.",
            tree_id=int(row["tree_id"]),
            task_id=task_id,
        )
    else:
        _resolve_task_alerts(db, task_id)
    _refresh_project_alerts(db, project_id)
    db.commit()
    return {"status": "ok"}


@router.get("/tasks/review-queue")
def task_review_queue(
    project_id: int,
    assignee_name: str | None = None,
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            SELECT t.id, t.tree_id, t.task_type, t.assignee_name, t.status, t.review_state,
                   t.priority, t.due_date, t.notes, t.photo_url, t.submitted_at, t.created_at,
                   t.reported_tree_status, t.review_notes,
                   tr.project_id, tr.status AS tree_status
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE tr.project_id = :project_id
              AND LOWER(t.review_state) = 'submitted'
              AND (:assignee_name IS NULL OR t.assignee_name = :assignee_name)
            ORDER BY COALESCE(t.submitted_at, t.created_at) DESC, t.id DESC
        """),
        {"project_id": project_id, "assignee_name": assignee_name},
    ).mappings().all()
    return [dict(row) for row in rows]


@router.post("/tasks/{task_id}/submit")
def submit_task_for_review(
    task_id: int,
    db: Session = Depends(get_db),
    notes: str | None = Body(default=None),
    photo_url: str | None = Body(default=None),
    tree_status: str | None = Body(default=None),
    actor_name: str | None = Body(default=None),
):
    normalized_tree_status = _normalize_tree_status(tree_status) if tree_status is not None else None
    if normalized_tree_status is not None and normalized_tree_status not in TREE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="Invalid tree status")
    task = db.execute(
        text("""
            SELECT t.id, t.tree_id, t.task_type, t.status, t.review_state, t.notes, t.photo_url,
                   t.reported_tree_status,
                   tr.project_id
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE t.id = :task_id
        """),
        {"task_id": task_id},
    ).mappings().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if _normalize_name(task.get("review_state")) == "approved":
        raise HTTPException(status_code=409, detail="Task already approved and locked")
    merged_notes = notes if notes is not None else task.get("notes")
    merged_photo = photo_url if photo_url is not None else task.get("photo_url")
    evidence_ok, detail = _has_required_evidence(task.get("task_type"), merged_notes, merged_photo)
    if not evidence_ok:
        raise HTTPException(status_code=400, detail=detail)

    row = db.execute(
        text("""
            UPDATE tree_tasks
            SET status = 'done',
                notes = COALESCE(:notes, notes),
                photo_url = COALESCE(:photo_url, photo_url),
                reported_tree_status = COALESCE(:reported_tree_status, reported_tree_status),
                review_state = 'submitted',
                submitted_at = NOW(),
                reviewed_at = NULL,
                reviewed_by = NULL,
                review_notes = NULL,
                completed_at = COALESCE(completed_at, NOW())
            WHERE id = :task_id
            RETURNING id, tree_id, status, review_state, reported_tree_status
        """),
        {
            "task_id": task_id,
            "notes": notes,
            "photo_url": photo_url,
            "reported_tree_status": normalized_tree_status,
        },
    ).mappings().first()
    if merged_photo:
        db.execute(
            text("""
                UPDATE trees
                SET photo_url = COALESCE(:photo_url, photo_url)
                WHERE id = :tree_id
            """),
            {"photo_url": merged_photo or None, "tree_id": int(row["tree_id"])},
        )

    project_id = int(task["project_id"])
    db.execute(
        text("""
            INSERT INTO green_task_reviews (task_id, decision, reviewer_name, review_notes)
            VALUES (:task_id, 'submitted', :reviewer_name, :review_notes)
        """),
        {"task_id": task_id, "reviewer_name": actor_name, "review_notes": merged_notes},
    )
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="task",
        entity_id=task_id,
        action="task_submitted_for_review",
        actor=actor_name,
        details={"status": row.get("status"), "review_state": row.get("review_state")},
    )
    _record_alert(
        db,
        project_id=project_id,
        alert_type="task_submitted",
        severity="warning",
        message=f"Task #{task_id} is awaiting supervisor review.",
        tree_id=int(row["tree_id"]),
        task_id=task_id,
    )
    _refresh_project_alerts(db, project_id)
    db.commit()
    return {"status": "submitted", "task_id": task_id}


@router.post("/tasks/{task_id}/review")
def review_submitted_task(
    task_id: int,
    db: Session = Depends(get_db),
    decision: str = Body(...),
    reviewer_name: str = Body(default=""),
    review_notes: str = Body(default=""),
    season_mode: str | None = Body(default=None),
):
    decision_key = _normalize_name(decision)
    if decision_key not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Decision must be approve or reject")
    if decision_key == "reject" and not (review_notes or "").strip():
        raise HTTPException(status_code=400, detail="Review note is required when rejecting a task")

    task = db.execute(
        text("""
            SELECT t.id, t.tree_id, t.task_type, t.assignee_name, t.status, t.review_state, t.model_season,
                   t.reported_tree_status,
                   tr.project_id
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE t.id = :task_id
        """),
        {"task_id": task_id},
    ).mappings().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if _normalize_name(task.get("review_state")) != "submitted":
        raise HTTPException(status_code=409, detail="Task is not in submitted state")

    project_id = int(task["project_id"])
    auto_generated_task_id = None
    if decision_key == "approve":
        db.execute(
            text("""
                UPDATE tree_tasks
                SET review_state = 'approved',
                    reviewed_at = NOW(),
                    reviewed_by = :reviewer_name,
                    review_notes = :review_notes,
                    completed_at = COALESCE(completed_at, NOW())
                WHERE id = :task_id
            """),
            {
                "task_id": task_id,
                "reviewer_name": reviewer_name or None,
                "review_notes": review_notes or None,
            },
        )
        reported_tree_status = _normalize_tree_status(task.get("reported_tree_status"))
        if reported_tree_status in TREE_STATUS_VALUES:
            db.execute(
                text("""
                    UPDATE trees
                    SET status = :status
                    WHERE id = :tree_id
                """),
                {"status": reported_tree_status, "tree_id": int(task["tree_id"])},
            )
        auto_generated_task_id = _auto_schedule_next_cycle(db, task_id, season_hint=season_mode or task.get("model_season"))
        _resolve_task_alerts(db, task_id)
        action_name = "task_review_approved"
    else:
        db.execute(
            text("""
                UPDATE tree_tasks
                SET status = 'pending',
                    review_state = 'rejected',
                    submitted_at = NULL,
                    completed_at = NULL,
                    reviewed_at = NOW(),
                    reviewed_by = :reviewer_name,
                    review_notes = :review_notes
                WHERE id = :task_id
            """),
            {
                "task_id": task_id,
                "reviewer_name": reviewer_name or None,
                "review_notes": review_notes or None,
            },
        )
        action_name = "task_review_rejected"

    db.execute(
        text("""
            INSERT INTO green_task_reviews (task_id, decision, reviewer_name, review_notes)
            VALUES (:task_id, :decision, :reviewer_name, :review_notes)
        """),
        {
            "task_id": task_id,
            "decision": "approved" if decision_key == "approve" else "rejected",
            "reviewer_name": reviewer_name or None,
            "review_notes": review_notes or None,
        },
    )
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="task",
        entity_id=task_id,
        action=action_name,
        actor=reviewer_name or None,
        details={
            "decision": decision_key,
            "auto_generated_task_id": auto_generated_task_id,
            "review_notes": review_notes or None,
        },
    )
    _refresh_project_alerts(db, project_id)
    db.commit()
    return {
        "status": "ok",
        "decision": "approved" if decision_key == "approve" else "rejected",
        "auto_generated_task_id": auto_generated_task_id,
    }


@router.post("/tasks/{task_id}/reopen")
def reopen_approved_task(
    task_id: int,
    db: Session = Depends(get_db),
    reviewer_name: str = Body(default=""),
    reason: str = Body(default=""),
):
    task = db.execute(
        text("""
            SELECT t.id, t.tree_id, t.review_state, tr.project_id
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE t.id = :task_id
        """),
        {"task_id": task_id},
    ).mappings().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if _normalize_name(task.get("review_state")) != "approved":
        raise HTTPException(status_code=409, detail="Only approved tasks can be reopened")

    db.execute(
        text("""
            UPDATE tree_tasks
            SET status = 'pending',
                review_state = 'reopened',
                submitted_at = NULL,
                completed_at = NULL,
                reviewed_at = NOW(),
                reviewed_by = :reviewer_name,
                review_notes = :review_notes
            WHERE id = :task_id
        """),
        {"task_id": task_id, "reviewer_name": reviewer_name or None, "review_notes": reason or None},
    )
    db.execute(
        text("""
            INSERT INTO green_task_reviews (task_id, decision, reviewer_name, review_notes)
            VALUES (:task_id, 'reopened', :reviewer_name, :review_notes)
        """),
        {"task_id": task_id, "reviewer_name": reviewer_name or None, "review_notes": reason or None},
    )
    project_id = int(task["project_id"])
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="task",
        entity_id=task_id,
        action="task_reopened",
        actor=reviewer_name or None,
        details={"reason": reason or None},
    )
    _refresh_project_alerts(db, project_id)
    db.commit()
    return {"status": "ok"}


@router.get("/projects/{project_id}/alerts")
def project_alerts(
    project_id: int,
    refresh: bool = Query(default=True),
    status: str = Query(default="open"),
    db: Session = Depends(get_db),
):
    if refresh:
        _refresh_project_alerts(db, project_id)
        db.commit()
    rows = db.execute(
        text("""
            SELECT id, project_id, tree_id, task_id, alert_type, severity, message, status,
                   payload, created_at, resolved_at
            FROM green_alerts
            WHERE project_id = :project_id
              AND (:status = 'all' OR status = :status)
            ORDER BY created_at DESC, id DESC
            LIMIT 300
        """),
        {"project_id": project_id, "status": status},
    ).mappings().all()
    items = [dict(row) for row in rows]
    summary = {
        "total": len(items),
        "danger": sum(1 for item in items if _normalize_name(item.get("severity")) == "danger"),
        "warning": sum(1 for item in items if _normalize_name(item.get("severity")) == "warning"),
        "info": sum(1 for item in items if _normalize_name(item.get("severity")) == "info"),
    }
    return {"project_id": project_id, "status_filter": status, "summary": summary, "items": items}


def _compare_metric(metric_value: float, comparator: str, threshold: float) -> bool:
    op = _normalize_name(comparator)
    if op == "gt":
        return metric_value > threshold
    if op == "gte":
        return metric_value >= threshold
    if op == "lt":
        return metric_value < threshold
    if op == "lte":
        return metric_value <= threshold
    if op == "eq":
        return metric_value == threshold
    return False


@router.get("/reports/kpi")
def reports_kpi(
    project_id: int = Query(...),
    days: int = Query(default=30, ge=1, le=365),
    snapshot: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    metrics = _compute_kpi_snapshot(project_id, db)
    if snapshot:
        _store_kpi_snapshot(project_id, metrics, db)
        db.commit()

    rows = db.execute(
        text("""
            SELECT id, project_id, snapshot_at, metrics
            FROM green_kpi_snapshots
            WHERE project_id = :project_id
              AND snapshot_at >= NOW() - (:days || ' days')::interval
            ORDER BY snapshot_at ASC
        """),
        {"project_id": project_id, "days": int(days)},
    ).mappings().all()
    trend = [dict(row) for row in rows]
    return {
        "project_id": project_id,
        "current": metrics,
        "trend_days": days,
        "trend": trend,
    }


@router.get("/reports/schedule")
def list_report_schedules(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            SELECT id, project_id, report_type, report_format, recipients, cron_expr, timezone, webhook_url,
                   is_enabled, created_by, last_run_at, next_run_at, created_at, updated_at
            FROM green_scheduled_reports
            WHERE project_id = :project_id
            ORDER BY created_at DESC, id DESC
        """),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]


@router.post("/reports/schedule")
def create_report_schedule(
    project_id: int = Body(...),
    report_type: str = Body(default="donor"),
    report_format: str = Body(default="pdf"),
    recipients: str = Body(default=""),
    cron_expr: str | None = Body(default=None),
    timezone: str = Body(default="Africa/Lagos"),
    webhook_url: str | None = Body(default=None),
    created_by: str | None = Body(default=None),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("""
            INSERT INTO green_scheduled_reports (
                project_id, report_type, report_format, recipients, cron_expr, timezone, webhook_url, created_by
            )
            VALUES (
                :project_id, :report_type, :report_format, :recipients, :cron_expr, :timezone, :webhook_url, :created_by
            )
            RETURNING id, project_id, report_type, report_format, recipients, cron_expr, timezone, webhook_url,
                      is_enabled, created_by, last_run_at, next_run_at, created_at, updated_at
        """),
        {
            "project_id": project_id,
            "report_type": (_normalize_name(report_type) or "donor"),
            "report_format": (_normalize_name(report_format) or "pdf"),
            "recipients": recipients or "",
            "cron_expr": cron_expr,
            "timezone": timezone or "Africa/Lagos",
            "webhook_url": webhook_url,
            "created_by": created_by,
        },
    ).mappings().first()
    db.commit()
    return dict(row)


@router.patch("/reports/schedule/{schedule_id}")
def update_report_schedule(
    schedule_id: int,
    report_type: str | None = Body(default=None),
    report_format: str | None = Body(default=None),
    recipients: str | None = Body(default=None),
    cron_expr: str | None = Body(default=None),
    timezone: str | None = Body(default=None),
    webhook_url: str | None = Body(default=None),
    is_enabled: bool | None = Body(default=None),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("""
            UPDATE green_scheduled_reports
            SET report_type = COALESCE(:report_type, report_type),
                report_format = COALESCE(:report_format, report_format),
                recipients = COALESCE(:recipients, recipients),
                cron_expr = COALESCE(:cron_expr, cron_expr),
                timezone = COALESCE(:timezone, timezone),
                webhook_url = COALESCE(:webhook_url, webhook_url),
                is_enabled = COALESCE(:is_enabled, is_enabled),
                updated_at = NOW()
            WHERE id = :schedule_id
            RETURNING id, project_id, report_type, report_format, recipients, cron_expr, timezone, webhook_url,
                      is_enabled, created_by, last_run_at, next_run_at, created_at, updated_at
        """),
        {
            "schedule_id": schedule_id,
            "report_type": _normalize_name(report_type) if report_type is not None else None,
            "report_format": _normalize_name(report_format) if report_format is not None else None,
            "recipients": recipients,
            "cron_expr": cron_expr,
            "timezone": timezone,
            "webhook_url": webhook_url,
            "is_enabled": is_enabled,
        },
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.commit()
    return dict(row)


@router.delete("/reports/schedule/{schedule_id}")
def delete_report_schedule(schedule_id: int, db: Session = Depends(get_db)):
    deleted = db.execute(
        text("DELETE FROM green_scheduled_reports WHERE id = :schedule_id RETURNING id"),
        {"schedule_id": schedule_id},
    ).scalar()
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.commit()
    return {"status": "ok", "id": int(deleted)}


@router.get("/alerts/rules")
def list_alert_rules(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            SELECT id, project_id, rule_name, metric_key, comparator, threshold, severity,
                   message_template, is_enabled, created_by, created_at, updated_at
            FROM green_alert_rules
            WHERE project_id = :project_id
            ORDER BY created_at DESC, id DESC
        """),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]


@router.post("/alerts/rules")
def create_alert_rule(
    project_id: int = Body(...),
    rule_name: str = Body(...),
    metric_key: str = Body(...),
    comparator: str = Body(default="gte"),
    threshold: float = Body(...),
    severity: str = Body(default="warning"),
    message_template: str | None = Body(default=None),
    created_by: str | None = Body(default=None),
    db: Session = Depends(get_db),
):
    cmp_key = _normalize_name(comparator)
    if cmp_key not in {"gt", "gte", "lt", "lte", "eq"}:
        raise HTTPException(status_code=400, detail="Invalid comparator")
    sev = _normalize_name(severity) or "warning"
    if sev not in {"info", "warning", "danger"}:
        sev = "warning"
    row = db.execute(
        text("""
            INSERT INTO green_alert_rules (
                project_id, rule_name, metric_key, comparator, threshold, severity, message_template, created_by
            )
            VALUES (
                :project_id, :rule_name, :metric_key, :comparator, :threshold, :severity, :message_template, :created_by
            )
            RETURNING id, project_id, rule_name, metric_key, comparator, threshold, severity,
                      message_template, is_enabled, created_by, created_at, updated_at
        """),
        {
            "project_id": project_id,
            "rule_name": rule_name.strip(),
            "metric_key": _normalize_name(metric_key),
            "comparator": cmp_key,
            "threshold": threshold,
            "severity": sev,
            "message_template": message_template,
            "created_by": created_by,
        },
    ).mappings().first()
    db.commit()
    return dict(row)


@router.patch("/alerts/rules/{rule_id}")
def update_alert_rule(
    rule_id: int,
    rule_name: str | None = Body(default=None),
    metric_key: str | None = Body(default=None),
    comparator: str | None = Body(default=None),
    threshold: float | None = Body(default=None),
    severity: str | None = Body(default=None),
    message_template: str | None = Body(default=None),
    is_enabled: bool | None = Body(default=None),
    db: Session = Depends(get_db),
):
    cmp_key = _normalize_name(comparator) if comparator is not None else None
    if cmp_key is not None and cmp_key not in {"gt", "gte", "lt", "lte", "eq"}:
        raise HTTPException(status_code=400, detail="Invalid comparator")
    sev = _normalize_name(severity) if severity is not None else None
    if sev is not None and sev not in {"info", "warning", "danger"}:
        raise HTTPException(status_code=400, detail="Invalid severity")
    row = db.execute(
        text("""
            UPDATE green_alert_rules
            SET rule_name = COALESCE(:rule_name, rule_name),
                metric_key = COALESCE(:metric_key, metric_key),
                comparator = COALESCE(:comparator, comparator),
                threshold = COALESCE(:threshold, threshold),
                severity = COALESCE(:severity, severity),
                message_template = COALESCE(:message_template, message_template),
                is_enabled = COALESCE(:is_enabled, is_enabled),
                updated_at = NOW()
            WHERE id = :rule_id
            RETURNING id, project_id, rule_name, metric_key, comparator, threshold, severity,
                      message_template, is_enabled, created_by, created_at, updated_at
        """),
        {
            "rule_id": rule_id,
            "rule_name": rule_name,
            "metric_key": _normalize_name(metric_key) if metric_key is not None else None,
            "comparator": cmp_key,
            "threshold": threshold,
            "severity": sev,
            "message_template": message_template,
            "is_enabled": is_enabled,
        },
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.commit()
    return dict(row)


@router.post("/alerts/evaluate")
def evaluate_alert_rules(
    project_id: int = Body(...),
    db: Session = Depends(get_db),
):
    metrics = _compute_kpi_snapshot(project_id, db)
    rules = db.execute(
        text("""
            SELECT id, rule_name, metric_key, comparator, threshold, severity, message_template
            FROM green_alert_rules
            WHERE project_id = :project_id
              AND is_enabled = TRUE
            ORDER BY id ASC
        """),
        {"project_id": project_id},
    ).mappings().all()
    webhook_targets = [
        str(row.get("webhook_url") or "").strip()
        for row in db.execute(
            text(
                """
                SELECT DISTINCT webhook_url
                FROM green_scheduled_reports
                WHERE project_id = :project_id
                  AND is_enabled = TRUE
                  AND webhook_url IS NOT NULL
                  AND TRIM(webhook_url) <> ''
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
    ]
    created_events: list[dict] = []
    created_deliveries = 0
    for rule in rules:
        metric_key = _normalize_name(rule.get("metric_key"))
        raw_value = metrics.get(metric_key)
        if raw_value is None:
            continue
        metric_value = float(raw_value)
        threshold = float(rule.get("threshold") or 0)
        if not _compare_metric(metric_value, rule.get("comparator"), threshold):
            continue
        msg = (rule.get("message_template") or "").strip()
        if not msg:
            msg = f"{rule.get('rule_name')}: {metric_key}={metric_value} vs threshold {threshold} ({rule.get('comparator')})."
        event = db.execute(
            text("""
                INSERT INTO green_alert_events (
                    project_id, rule_id, severity, metric_key, metric_value, threshold, message, payload
                )
                VALUES (
                    :project_id, :rule_id, :severity, :metric_key, :metric_value, :threshold, :message, CAST(:payload AS JSONB)
                )
                RETURNING id, project_id, rule_id, severity, status, metric_key, metric_value, threshold,
                          message, payload, triggered_at, resolved_at
            """),
            {
                "project_id": project_id,
                "rule_id": int(rule["id"]),
                "severity": _normalize_name(rule.get("severity") or "warning"),
                "metric_key": metric_key,
                "metric_value": metric_value,
                "threshold": threshold,
                "message": msg,
                "payload": _safe_json({"metrics": metrics}),
            },
        ).mappings().first()
        event_payload = dict(event)
        created_events.append(event_payload)
        event_id = int(event_payload.get("id") or 0)
        if event_id > 0 and webhook_targets:
            for target_url in webhook_targets:
                db.execute(
                    text(
                        """
                        INSERT INTO green_webhook_deliveries (event_id, target_url, status, attempt_count)
                        VALUES (:event_id, :target_url, 'pending', 0)
                        """
                    ),
                    {"event_id": event_id, "target_url": target_url},
                )
                created_deliveries += 1
    db.commit()
    return {
        "project_id": project_id,
        "created": len(created_events),
        "events": created_events,
        "webhook_deliveries_created": created_deliveries,
    }


@router.get("/alerts/events")
def list_alert_events(
    project_id: int = Query(...),
    status: str = Query(default="all"),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            SELECT id, project_id, rule_id, severity, status, metric_key, metric_value, threshold,
                   message, payload, triggered_at, resolved_at
            FROM green_alert_events
            WHERE project_id = :project_id
              AND (:status = 'all' OR status = :status)
            ORDER BY triggered_at DESC, id DESC
            LIMIT 500
        """),
        {"project_id": project_id, "status": status},
    ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/alerts/webhook-deliveries")
def list_webhook_deliveries(
    project_id: int = Query(...),
    status: str = Query(default="all"),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text(
            """
            SELECT d.id, d.event_id, d.target_url, d.status, d.response_code, d.response_body,
                   d.attempt_count, d.delivered_at, d.created_at,
                   e.rule_id, e.severity, e.metric_key, e.metric_value, e.threshold, e.message, e.triggered_at
            FROM green_webhook_deliveries d
            JOIN green_alert_events e ON e.id = d.event_id
            WHERE e.project_id = :project_id
              AND (:status = 'all' OR d.status = :status)
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT 500
            """
        ),
        {"project_id": project_id, "status": status},
    ).mappings().all()
    return [dict(row) for row in rows]


@router.patch("/alerts/webhook-deliveries/{delivery_id}")
def update_webhook_delivery(
    delivery_id: int,
    status: str | None = Body(default=None),
    response_code: int | None = Body(default=None),
    response_body: str | None = Body(default=None),
    increment_attempt: bool = Body(default=False),
    db: Session = Depends(get_db),
):
    status_key = _normalize_name(status) if status is not None else None
    if status_key is not None and status_key not in {"pending", "failed", "delivered"}:
        raise HTTPException(status_code=400, detail="Invalid delivery status")
    row = db.execute(
        text(
            """
            UPDATE green_webhook_deliveries
            SET status = COALESCE(:status, status),
                response_code = COALESCE(:response_code, response_code),
                response_body = COALESCE(:response_body, response_body),
                attempt_count = CASE WHEN :increment_attempt THEN attempt_count + 1 ELSE attempt_count END,
                delivered_at = CASE
                    WHEN COALESCE(:status, status) = 'delivered' THEN NOW()
                    ELSE delivered_at
                END
            WHERE id = :delivery_id
            RETURNING id, event_id, target_url, status, response_code, response_body,
                      attempt_count, delivered_at, created_at
            """
        ),
        {
            "delivery_id": delivery_id,
            "status": status_key,
            "response_code": response_code,
            "response_body": response_body,
            "increment_attempt": bool(increment_attempt),
        },
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    db.commit()
    return dict(row)


@router.get("/projects/{project_id}/audit-events")
def project_audit_events(
    project_id: int,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("""
            SELECT id, project_id, entity_type, entity_id, action, actor, details, created_at
            FROM green_audit_events
            WHERE project_id = :project_id
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
        """),
        {"project_id": project_id, "limit": limit},
    ).mappings().all()
    return [dict(row) for row in rows]


@router.get("/projects/{project_id}/live-maintenance")
def live_maintenance_rows(
    project_id: int,
    season_mode: str = Query(default="rainy"),
    assignee_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    payload = _compute_live_maintenance_rows(
        db=db,
        project_id=project_id,
        season_mode=season_mode,
        assignee_name=assignee_name,
    )
    return {
        "project_id": project_id,
        "season_mode": "dry" if _normalize_name(season_mode) == "dry" else "rainy",
        "computed_at": datetime.utcnow().isoformat(),
        "summary": payload["summary"],
        "rows": payload["rows"],
        "sources": LIVE_SOURCE_REFERENCES,
    }


def _build_donor_report_rows(project_id: int, db: Session) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT t.id AS task_id, t.tree_id, tr.species, t.assignee_name, t.task_type, t.priority,
                   t.status, t.review_state, t.due_date, t.created_at, t.submitted_at, t.reviewed_at,
                   t.reviewed_by, t.review_notes, t.completed_at, t.notes, t.photo_url,
                   t.reported_tree_status, tr.status AS tree_status
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE tr.project_id = :project_id
            ORDER BY COALESCE(t.reviewed_at, t.submitted_at, t.created_at) DESC, t.id DESC
        """),
        {"project_id": project_id},
    ).mappings().all()
    report_rows: list[dict] = []
    today = date.today()
    for row in rows:
        due_date = _parse_date_value(row.get("due_date"))
        completed_date = _parse_date_value(row.get("completed_at"))
        if due_date and completed_date:
            delay_days = _day_diff(completed_date, due_date)
        elif due_date and not completed_date:
            delay_days = _day_diff(today, due_date)
        else:
            delay_days = None
        evidence_ok, _ = _has_required_evidence(row.get("task_type"), row.get("notes"), row.get("photo_url"))
        item = dict(row)
        item["evidence_status"] = "complete" if evidence_ok else "missing"
        item["delay_days"] = delay_days
        report_rows.append(item)
    return report_rows


def _review_summary_by_tree(project_id: int, db: Session, assignee_name: str | None = None) -> dict[int, dict]:
    rows = db.execute(
        text("""
            SELECT tr.id AS tree_id,
                   SUM(CASE WHEN LOWER(COALESCE(t.review_state, 'none')) = 'submitted' THEN 1 ELSE 0 END) AS submitted_count,
                   SUM(CASE WHEN LOWER(COALESCE(t.review_state, 'none')) = 'approved' THEN 1 ELSE 0 END) AS approved_count,
                   SUM(CASE WHEN LOWER(COALESCE(t.review_state, 'none')) = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                   MAX(t.submitted_at) AS last_submitted_at,
                   MAX(t.reviewed_at) AS last_reviewed_at,
                   (ARRAY_AGG(t.review_notes ORDER BY COALESCE(t.reviewed_at, t.submitted_at, t.created_at) DESC NULLS LAST, t.id DESC))[1]
                       AS last_review_note,
                   (ARRAY_AGG(t.review_state ORDER BY COALESCE(t.reviewed_at, t.submitted_at, t.created_at) DESC NULLS LAST, t.id DESC))[1]
                       AS last_review_state
            FROM trees tr
            LEFT JOIN tree_tasks t ON t.tree_id = tr.id
            WHERE tr.project_id = :project_id
              AND (:assignee_name IS NULL OR tr.created_by = :assignee_name)
            GROUP BY tr.id
        """),
        {"project_id": project_id, "assignee_name": assignee_name},
    ).mappings().all()
    result: dict[int, dict] = {}
    for row in rows:
        tree_id = int(row.get("tree_id"))
        result[tree_id] = {
            "review_submitted": int(row.get("submitted_count") or 0),
            "review_approved": int(row.get("approved_count") or 0),
            "review_rejected": int(row.get("rejected_count") or 0),
            "last_submitted_at": row.get("last_submitted_at"),
            "last_reviewed_at": row.get("last_reviewed_at"),
            "last_review_note": row.get("last_review_note") or "",
            "last_review_state": row.get("last_review_state") or "",
        }
    return result


def _compute_kpi_snapshot(project_id: int, db: Session) -> dict:
    tree_rows = db.execute(
        text("SELECT status FROM trees WHERE project_id = :project_id"),
        {"project_id": project_id},
    ).mappings().all()
    task_rows = db.execute(
        text("""
            SELECT t.status, t.review_state, t.due_date, t.notes, t.photo_url, t.task_type
            FROM tree_tasks t
            JOIN trees tr ON tr.id = t.tree_id
            WHERE tr.project_id = :project_id
        """),
        {"project_id": project_id},
    ).mappings().all()

    total_trees = len(tree_rows)
    healthy_trees = sum(1 for row in tree_rows if _normalize_tree_status(row.get("status")) in HEALTHY_TREE_STATUSES)
    dead_trees = sum(1 for row in tree_rows if _normalize_tree_status(row.get("status")) in DEAD_TREE_STATUSES)
    attention_trees = sum(1 for row in tree_rows if _normalize_tree_status(row.get("status")) in ATTENTION_TREE_STATUSES)
    pending_planting = sum(1 for row in tree_rows if _normalize_tree_status(row.get("status")) == "pending_planting")
    survival_rate = round((healthy_trees / total_trees) * 100, 1) if total_trees else 0.0

    today = date.today()
    total_tasks = len(task_rows)
    submitted_tasks = 0
    approved_tasks = 0
    rejected_tasks = 0
    open_tasks = 0
    overdue_tasks = 0
    evidence_complete = 0
    for task in task_rows:
        state = _normalize_name(task.get("review_state") or "none")
        status = _normalize_name(task.get("status") or "pending")
        due = _parse_date_value(task.get("due_date"))
        if state == "submitted":
            submitted_tasks += 1
        if state == "approved":
            approved_tasks += 1
        if state == "rejected":
            rejected_tasks += 1
        if not (_is_done_status(status) and state in {"approved", "none"}):
            open_tasks += 1
        if due and due < today and not (_is_done_status(status) and state in {"approved", "none"}):
            overdue_tasks += 1
        evidence_ok, _ = _has_required_evidence(task.get("task_type"), task.get("notes"), task.get("photo_url"))
        if evidence_ok:
            evidence_complete += 1

    evidence_rate = round((evidence_complete / total_tasks) * 100, 1) if total_tasks else 0.0
    return {
        "project_id": project_id,
        "snapshot_date": datetime.utcnow().isoformat(),
        "trees_total": total_trees,
        "trees_healthy": healthy_trees,
        "trees_dead_or_removed": dead_trees,
        "trees_attention": attention_trees,
        "trees_pending_planting": pending_planting,
        "survival_rate": survival_rate,
        "tasks_total": total_tasks,
        "tasks_open": open_tasks,
        "tasks_submitted": submitted_tasks,
        "tasks_approved": approved_tasks,
        "tasks_rejected": rejected_tasks,
        "tasks_overdue": overdue_tasks,
        "evidence_complete_rate": evidence_rate,
    }


def _store_kpi_snapshot(project_id: int, metrics: dict, db: Session):
    db.execute(
        text("""
            INSERT INTO green_kpi_snapshots (project_id, metrics)
            VALUES (:project_id, CAST(:metrics AS JSONB))
        """),
        {"project_id": project_id, "metrics": _safe_json(metrics)},
    )


@router.get("/projects/{project_id}/donor-report/csv")
def export_donor_report_csv(project_id: int, db: Session = Depends(get_db)):
    project = get_project(project_id, db)
    rows = db.execute(text("""
        SELECT id, project_id, species, planting_date, status, notes, photo_url, created_by, created_at,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
    """), {"project_id": project_id}).mappings().all()
    maintenance_rows = _maintenance_summary_by_tree(project_id, db)
    rows = _attach_maintenance_to_tree_rows(rows, maintenance_rows)
    review_summary = _review_summary_by_tree(project_id, db)
    kpi = _compute_kpi_snapshot(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_donor_report.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["LandCheck Donor + Operations Report"])
        writer.writerow(["Project", project.get("name") or "", "Location", project.get("location_text") or "", "Sponsor", project.get("sponsor") or ""])
        writer.writerow([
            "KPI",
            f"Trees {kpi.get('trees_total', 0)}",
            f"Healthy {kpi.get('trees_healthy', 0)}",
            f"Attention {kpi.get('trees_attention', 0)}",
            f"Open Tasks {kpi.get('tasks_open', 0)}",
            f"Submitted {kpi.get('tasks_submitted', 0)}",
            f"Rejected {kpi.get('tasks_rejected', 0)}",
            f"Overdue {kpi.get('tasks_overdue', 0)}",
        ])
        writer.writerow([])
        writer.writerow([
            "tree_id",
            "project_id",
            "lng",
            "lat",
            "species",
            "planting_date",
            "tree_status",
            "created_by",
            "maintenance_count",
            "maintenance_done",
            "maintenance_pending",
            "maintenance_overdue",
            "maintenance_types",
            "last_maintenance_type",
            "last_maintenance_date",
            "review_submitted",
            "review_approved",
            "review_rejected",
            "last_review_state",
            "last_review_note",
            "last_submitted_at",
            "last_reviewed_at",
            "tree_notes",
            "tree_photo_url",
            "tree_created_at",
        ])
        for row in rows:
            tree_id = int(row.get("id"))
            review = review_summary.get(tree_id, {})
            writer.writerow([
                row.get("id"),
                row.get("project_id"),
                row.get("lng"),
                row.get("lat"),
                row.get("species"),
                row.get("planting_date"),
                row.get("status"),
                row.get("created_by"),
                row.get("maintenance_count"),
                row.get("maintenance_done"),
                row.get("maintenance_pending"),
                row.get("maintenance_overdue"),
                row.get("maintenance_types"),
                row.get("last_maintenance_type"),
                row.get("last_maintenance_date"),
                review.get("review_submitted", 0),
                review.get("review_approved", 0),
                review.get("review_rejected", 0),
                review.get("last_review_state", ""),
                review.get("last_review_note", ""),
                review.get("last_submitted_at", ""),
                review.get("last_reviewed_at", ""),
                row.get("notes"),
                row.get("photo_url"),
                row.get("created_at"),
            ])

        writer.writerow([])
        writer.writerow(["Recent Task Review Timeline"])
        writer.writerow([
            "task_id",
            "tree_id",
            "species",
            "assignee_name",
            "task_type",
            "priority",
            "status",
            "review_state",
            "due_date",
            "completed_at",
            "submitted_at",
            "reviewed_at",
            "reviewed_by",
            "review_notes",
            "delay_days",
            "evidence_status",
            "reported_tree_status",
            "tree_status",
            "photo_url",
            "notes",
        ])
        donor_rows = _build_donor_report_rows(project_id, db)
        for row in donor_rows:
            writer.writerow([
                row.get("task_id"),
                row.get("tree_id"),
                row.get("species"),
                row.get("assignee_name"),
                row.get("task_type"),
                row.get("priority"),
                row.get("status"),
                row.get("review_state"),
                row.get("due_date"),
                row.get("completed_at"),
                row.get("submitted_at"),
                row.get("reviewed_at"),
                row.get("reviewed_by"),
                row.get("review_notes"),
                row.get("delay_days"),
                row.get("evidence_status"),
                row.get("reported_tree_status"),
                row.get("tree_status"),
                row.get("photo_url"),
                row.get("notes"),
            ])

    filename = f"project_{project_id}_donor_report.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/projects/{project_id}/donor-report/pdf")
def export_donor_report_pdf(project_id: int, db: Session = Depends(get_db)):
    # Use the comprehensive map report and include donor/review details in additional pages.
    return export_work_report_pdf(project_id=project_id, assignee_name=None, db=db)


@router.get("/donor/export/csv")
def export_donor_report_csv_alias(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
):
    return export_donor_report_csv(project_id=project_id, db=db)


@router.get("/donor/export/pdf")
def export_donor_report_pdf_alias(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
):
    return export_donor_report_pdf(project_id=project_id, db=db)


@router.get("/trees/{tree_id}/timeline")
def tree_timeline(tree_id: int, db: Session = Depends(get_db)):
    tree = db.execute(text("""
        SELECT id, species, planting_date, status, notes, photo_url, created_by, created_at
        FROM trees
        WHERE id = :tree_id
    """), {"tree_id": tree_id}).mappings().first()
    tasks = list_tree_tasks(tree_id, db)
    visits = db.execute(text("""
        SELECT visit_date, status, notes, photo_url, created_by, created_at
        FROM tree_visits
        WHERE tree_id = :tree_id
        ORDER BY visit_date DESC
    """), {"tree_id": tree_id}).mappings().all()
    return {
        "tree": dict(tree) if tree else None,
        "tasks": tasks,
        "visits": [dict(v) for v in visits],
    }


@router.get("/projects/{project_id}/task-stats")
def task_stats(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            COUNT(*) AS total,
            SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) IN ('done', 'completed', 'closed')
                         AND LOWER(COALESCE(review_state, 'none')) IN ('approved', 'none')
                    THEN 1 ELSE 0
                END
            ) AS done,
            SUM(
                CASE
                    WHEN NOT (
                        LOWER(COALESCE(status, '')) IN ('done', 'completed', 'closed')
                        AND LOWER(COALESCE(review_state, 'none')) IN ('approved', 'none')
                    ) AND LOWER(COALESCE(status, 'pending')) <> 'overdue'
                    THEN 1 ELSE 0
                END
            ) AS pending,
            SUM(
                CASE
                    WHEN LOWER(COALESCE(status, 'pending')) = 'overdue'
                    THEN 1 ELSE 0
                END
            ) AS overdue
        FROM tree_tasks
        WHERE tree_id IN (SELECT id FROM trees WHERE project_id = :project_id)
    """), {"project_id": project_id}).mappings().first()
    return dict(rows)


@router.post("/work-orders")
def create_work_order(
    db: Session = Depends(get_db),
    project_id: int = Body(...),
    assignee_name: str = Body(...),
    work_type: str = Body(...),
    target_trees: int = Body(default=0),
    maintenance_schedule: str = Body(default=""),
    due_date: str | None = Body(default=None),
):
    if work_type not in {"planting", "maintenance"}:
        raise HTTPException(status_code=400, detail="Invalid work_type")
    row = db.execute(text("""
        INSERT INTO green_work_orders (
            project_id, assignee_name, work_type, target_trees, maintenance_schedule, due_date
        )
        VALUES (:project_id, :assignee_name, :work_type, :target_trees, :maintenance_schedule, :due_date)
        RETURNING id
    """), {
        "project_id": project_id,
        "assignee_name": assignee_name,
        "work_type": work_type,
        "target_trees": target_trees,
        "maintenance_schedule": maintenance_schedule or None,
        "due_date": due_date,
    }).scalar()
    _log_audit_event(
        db,
        project_id=project_id,
        entity_type="work_order",
        entity_id=int(row),
        action="work_order_created",
        actor=assignee_name,
        details={
            "work_type": work_type,
            "target_trees": target_trees,
            "maintenance_schedule": maintenance_schedule or None,
            "due_date": due_date,
        },
    )
    db.commit()
    return {"id": row}


@router.get("/work-orders")
def list_work_orders(
    project_id: int,
    assignee_name: str | None = None,
    db: Session = Depends(get_db),
):
    rows = db.execute(text("""
        WITH tree_counts AS (
            SELECT created_by AS assignee_name, COUNT(*) AS planted
            FROM trees
            WHERE project_id = :project_id
            GROUP BY created_by
        )
        SELECT o.id, o.project_id, o.assignee_name, o.work_type, o.target_trees,
               o.maintenance_schedule, o.due_date, o.status,
               CASE
                   WHEN o.work_type = 'planting' THEN COALESCE(t.planted, 0)
                   ELSE o.planted_count
               END AS planted_count,
               o.last_update, o.created_at
        FROM green_work_orders o
        LEFT JOIN tree_counts t ON t.assignee_name = o.assignee_name
        WHERE o.project_id = :project_id
          AND (:assignee_name IS NULL OR o.assignee_name = :assignee_name)
        ORDER BY o.created_at DESC
    """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/work-orders/{work_id}")
def update_work_order(
    work_id: int,
    db: Session = Depends(get_db),
    status: str | None = Body(default=None),
    planted_count: int | None = Body(default=None),
):
    # Auto-calc planted_count from trees created by assignee for planting orders.
    row = db.execute(text("""
        SELECT id, project_id, assignee_name, work_type, status
        FROM green_work_orders
        WHERE id = :work_id
    """), {"work_id": work_id}).mappings().first()

    planted_value = planted_count
    if row and row["work_type"] == "planting":
        planted_value = db.execute(text("""
            SELECT COUNT(*) FROM trees
            WHERE project_id = :project_id AND created_by = :assignee_name
        """), {"project_id": row["project_id"], "assignee_name": row["assignee_name"]}).scalar()

    existing_status = row.get("status") if row else None
    db.execute(text("""
        UPDATE green_work_orders
        SET status = COALESCE(:status, status),
            planted_count = COALESCE(:planted_count, planted_count),
            last_update = NOW()
        WHERE id = :work_id
    """), {
        "status": status,
        "planted_count": planted_value,
        "work_id": work_id,
    })
    if row:
        _log_audit_event(
            db,
            project_id=int(row["project_id"]),
            entity_type="work_order",
            entity_id=work_id,
            action="work_order_updated",
            actor=row.get("assignee_name"),
            details={
                "status": status,
                "planted_count": planted_value,
                "work_type": row.get("work_type"),
                "existing_status": existing_status,
            },
        )
    db.commit()
    return {"status": "ok"}


@router.get("/work-stats")
def work_stats(project_id: int, db: Session = Depends(get_db)):
    orders = db.execute(text("""
        SELECT assignee_name,
               COUNT(*) AS orders,
               SUM(target_trees) AS target_trees
        FROM green_work_orders
        WHERE project_id = :project_id
        GROUP BY assignee_name
    """), {"project_id": project_id}).mappings().all()

    tree_counts = db.execute(text("""
        SELECT created_by AS assignee_name, COUNT(*) AS trees_logged
        FROM trees
        WHERE project_id = :project_id
        GROUP BY created_by
    """), {"project_id": project_id}).mappings().all()
    tree_map = {r["assignee_name"]: r["trees_logged"] for r in tree_counts}
    merged = []
    for r in orders:
        row = dict(r)
        row["planted_count"] = tree_map.get(row.get("assignee_name"), 0)
        merged.append(row)

    maintenance_by_assignee = db.execute(text("""
        SELECT t.assignee_name,
               COUNT(*) AS maintenance_total,
               SUM(
                   CASE
                       WHEN LOWER(COALESCE(t.status, '')) IN ('done', 'completed', 'closed')
                            AND LOWER(COALESCE(t.review_state, 'none')) IN ('approved', 'none')
                       THEN 1 ELSE 0
                   END
               ) AS maintenance_done,
               SUM(
                   CASE
                       WHEN NOT (
                           LOWER(COALESCE(t.status, '')) IN ('done', 'completed', 'closed')
                           AND LOWER(COALESCE(t.review_state, 'none')) IN ('approved', 'none')
                       ) AND LOWER(COALESCE(t.status, 'pending')) <> 'overdue'
                       THEN 1 ELSE 0
                   END
               ) AS maintenance_pending,
               SUM(CASE WHEN t.status = 'overdue' THEN 1 ELSE 0 END) AS maintenance_overdue,
               COALESCE(STRING_AGG(DISTINCT t.task_type, ', ' ORDER BY t.task_type), '') AS maintenance_types,
               MAX(COALESCE(t.completed_at::date, t.due_date, t.created_at::date)) AS last_maintenance_date
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE tr.project_id = :project_id
        GROUP BY t.assignee_name
        ORDER BY t.assignee_name
    """), {"project_id": project_id}).mappings().all()

    maintenance_by_type = db.execute(text("""
        SELECT t.assignee_name,
               t.task_type,
               COUNT(*) AS maintenance_times,
               MAX(COALESCE(t.completed_at::date, t.due_date, t.created_at::date)) AS last_maintenance_date
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE tr.project_id = :project_id
        GROUP BY t.assignee_name, t.task_type
        ORDER BY t.assignee_name, maintenance_times DESC, t.task_type
    """), {"project_id": project_id}).mappings().all()

    return {
        "orders": merged,
        "trees_by_user": [dict(r) for r in tree_counts],
        "maintenance_by_assignee": [dict(r) for r in maintenance_by_assignee],
        "maintenance_by_type": [dict(r) for r in maintenance_by_type],
    }


@router.get("/work-stats/export/csv")
def export_work_stats_csv(project_id: int, db: Session = Depends(get_db)):
    stats = work_stats(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_work_stats.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["assignee", "orders", "target_trees", "planted_count"])
        for r in stats["orders"]:
            writer.writerow([
                r.get("assignee_name", ""),
                r.get("orders", 0),
                r.get("target_trees", 0),
                r.get("planted_count", 0),
            ])

    filename = f"project_{project_id}_work_stats.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/work-stats/export/pdf")
def export_work_stats_pdf(project_id: int, db: Session = Depends(get_db)):
    project = get_project(project_id, db)
    stats = work_stats(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_work_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()
    render_green_work_report_pdf(pdf_path, project, stats)
    filename = f"project_{project_id}_work_report.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/projects/{project_id}/tasks/export/csv")
def export_tasks_csv(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT t.id, t.task_type, t.assignee_name, t.due_date, t.priority, t.status,
               t.review_state, t.submitted_at, t.reviewed_at, t.reviewed_by, t.review_notes,
               t.model_season, t.source_task_id, t.auto_generated,
               t.notes, t.photo_url, t.created_at, t.completed_at
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE tr.project_id = :project_id
        ORDER BY t.created_at DESC
    """), {"project_id": project_id}).mappings().all()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_tasks.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "task_id", "task_type", "assignee_name", "due_date", "priority", "status",
            "review_state", "submitted_at", "reviewed_at", "reviewed_by", "review_notes",
            "model_season", "source_task_id", "auto_generated",
            "notes", "photo_url", "created_at", "completed_at"
        ])
        for r in rows:
            writer.writerow([
                r["id"], r["task_type"], r["assignee_name"], r["due_date"], r["priority"], r["status"],
                r["review_state"], r["submitted_at"], r["reviewed_at"], r["reviewed_by"], r["review_notes"],
                r["model_season"], r["source_task_id"], r["auto_generated"],
                r["notes"], r["photo_url"], r["created_at"], r["completed_at"],
            ])

    filename = f"project_{project_id}_tasks.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/projects/{project_id}/tasks/export/pdf")
def export_tasks_pdf(project_id: int, db: Session = Depends(get_db)):
    project = get_project(project_id, db)
    stats = task_stats(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_tasks_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()
    render_green_work_report_pdf(pdf_path, project, {"orders": [], **stats})
    filename = f"project_{project_id}_tasks_report.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/projects/{project_id}/export/csv")
def export_project_csv(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, project_id, species, planting_date, status, notes, photo_url, created_by, created_at,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
    """), {"project_id": project_id}).mappings().all()
    maintenance_rows = _maintenance_summary_by_tree(project_id, db)
    rows = _attach_maintenance_to_tree_rows(rows, maintenance_rows)
    review_summary = _review_summary_by_tree(project_id, db)
    kpi = _compute_kpi_snapshot(project_id, db)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_trees.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["LandCheck Project Export"])
        writer.writerow([
            "KPI",
            f"Trees {kpi.get('trees_total', 0)}",
            f"Healthy {kpi.get('trees_healthy', 0)}",
            f"Attention {kpi.get('trees_attention', 0)}",
            f"Open Tasks {kpi.get('tasks_open', 0)}",
            f"Submitted {kpi.get('tasks_submitted', 0)}",
            f"Rejected {kpi.get('tasks_rejected', 0)}",
        ])
        writer.writerow([])
        writer.writerow([
            "tree_id", "project_id", "lng", "lat", "species", "planting_date",
            "status", "notes", "photo_url", "created_by", "created_at",
            "maintenance_count", "maintenance_done", "maintenance_pending", "maintenance_overdue",
            "maintenance_types", "last_maintenance_type", "last_maintenance_date",
            "review_submitted", "review_approved", "review_rejected", "last_review_state", "last_review_note",
            "last_submitted_at", "last_reviewed_at"
        ])
        for r in rows:
            review = review_summary.get(int(r["id"]), {})
            writer.writerow([
                r["id"], r["project_id"], r["lng"], r["lat"], r["species"],
                r["planting_date"], r["status"], r["notes"], r["photo_url"],
                r["created_by"], r["created_at"],
                r.get("maintenance_count", 0), r.get("maintenance_done", 0), r.get("maintenance_pending", 0), r.get("maintenance_overdue", 0),
                r.get("maintenance_types", ""), r.get("last_maintenance_type", ""), r.get("last_maintenance_date", ""),
                review.get("review_submitted", 0), review.get("review_approved", 0), review.get("review_rejected", 0),
                review.get("last_review_state", ""), review.get("last_review_note", ""),
                review.get("last_submitted_at", ""), review.get("last_reviewed_at", ""),
            ])

    filename = f"project_{project_id}_trees.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


def _maintenance_summary_by_tree(project_id: int, db: Session, assignee_name: str | None = None) -> list[dict]:
    rows = db.execute(text("""
        SELECT tr.id AS tree_id,
               COUNT(t.id) AS maintenance_count,
               SUM(
                   CASE
                       WHEN LOWER(COALESCE(t.status, '')) IN ('done', 'completed', 'closed')
                            AND LOWER(COALESCE(t.review_state, 'none')) IN ('approved', 'none')
                       THEN 1 ELSE 0
                   END
               ) AS maintenance_done,
               SUM(
                   CASE
                       WHEN NOT (
                           LOWER(COALESCE(t.status, '')) IN ('done', 'completed', 'closed')
                           AND LOWER(COALESCE(t.review_state, 'none')) IN ('approved', 'none')
                       ) AND LOWER(COALESCE(t.status, 'pending')) <> 'overdue'
                       THEN 1 ELSE 0
                   END
               ) AS maintenance_pending,
               SUM(CASE WHEN t.status = 'overdue' THEN 1 ELSE 0 END) AS maintenance_overdue,
               COALESCE(STRING_AGG(DISTINCT t.task_type, ', ' ORDER BY t.task_type), '') AS maintenance_types,
               MAX(COALESCE(t.completed_at::date, t.due_date, t.created_at::date)) AS last_maintenance_date,
               (ARRAY_AGG(t.task_type ORDER BY COALESCE(t.completed_at, t.created_at) DESC NULLS LAST, t.id DESC))[1]
                 AS last_maintenance_type
        FROM trees tr
        LEFT JOIN tree_tasks t ON t.tree_id = tr.id
        WHERE tr.project_id = :project_id
          AND (:assignee_name IS NULL OR tr.created_by = :assignee_name)
        GROUP BY tr.id
        ORDER BY tr.id
    """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()

    cleaned = []
    for row in rows:
        item = dict(row)
        item["maintenance_count"] = int(item.get("maintenance_count") or 0)
        item["maintenance_done"] = int(item.get("maintenance_done") or 0)
        item["maintenance_pending"] = int(item.get("maintenance_pending") or 0)
        item["maintenance_overdue"] = int(item.get("maintenance_overdue") or 0)
        item["maintenance_types"] = item.get("maintenance_types") or ""
        item["last_maintenance_type"] = item.get("last_maintenance_type") or ""
        cleaned.append(item)
    return cleaned


def _attach_maintenance_to_tree_rows(rows: list[dict], summary_rows: list[dict]) -> list[dict]:
    summary_by_tree = {r["tree_id"]: r for r in summary_rows}
    merged = []
    for row in rows:
        item = dict(row)
        summary = summary_by_tree.get(item.get("id")) or {}
        item["maintenance_count"] = summary.get("maintenance_count", 0)
        item["maintenance_done"] = summary.get("maintenance_done", 0)
        item["maintenance_pending"] = summary.get("maintenance_pending", 0)
        item["maintenance_overdue"] = summary.get("maintenance_overdue", 0)
        item["maintenance_types"] = summary.get("maintenance_types", "")
        item["last_maintenance_type"] = summary.get("last_maintenance_type", "")
        item["last_maintenance_date"] = summary.get("last_maintenance_date")
        merged.append(item)
    return merged


@router.get("/projects/{project_id}/export/pdf")
def export_project_pdf(
    project_id: int,
    lng: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    zoom: float | None = Query(default=None),
    bearing: float | None = Query(default=0.0),
    pitch: float | None = Query(default=0.0),
    db: Session = Depends(get_db),
):
    def _coerce_optional_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            if isinstance(value, bool):
                return None
            return float(value)
        except Exception:
            return None

    lng_value = _coerce_optional_float(lng)
    lat_value = _coerce_optional_float(lat)
    zoom_value = _coerce_optional_float(zoom)
    bearing_value = _coerce_optional_float(bearing)
    pitch_value = _coerce_optional_float(pitch)

    project = get_project(project_id, db)
    rows = db.execute(text("""
        SELECT id, species, planting_date, status, notes,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
        LIMIT 200
    """), {"project_id": project_id}).mappings().all()
    map_rows = db.execute(text("""
        SELECT id, species, planting_date, status, notes,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
        LIMIT 1000
    """), {"project_id": project_id}).mappings().all()
    maintenance_rows = _maintenance_summary_by_tree(project_id, db)
    rows = _attach_maintenance_to_tree_rows(rows, maintenance_rows)
    map_rows = _attach_maintenance_to_tree_rows(map_rows, maintenance_rows)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_project_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    map_png = _build_report_map_png(
        map_rows=map_rows,
        lng=lng_value,
        lat=lat_value,
        zoom=zoom_value,
        bearing=bearing_value,
        pitch=pitch_value,
    )

    map_view = None
    if lng_value is not None and lat_value is not None and zoom_value is not None:
        map_view = {"lng": lng_value, "lat": lat_value, "zoom": zoom_value}
    donor_rows = _build_donor_report_rows(project_id, db)
    kpi_snapshot = _compute_kpi_snapshot(project_id, db)
    try:
        render_green_report_pdf(
            pdf_path,
            project,
            rows,
            map_png=map_png,
            map_rows=map_rows,
            map_view=map_view,
            maintenance_rows=maintenance_rows,
            donor_rows=donor_rows,
            kpi_snapshot=kpi_snapshot,
        )
    except Exception:
        # Hard fallback: regenerate without external map image bytes.
        render_green_report_pdf(
            pdf_path,
            project,
            rows,
            map_png=None,
            map_rows=map_rows,
            map_view=map_view,
            maintenance_rows=maintenance_rows,
            donor_rows=donor_rows,
            kpi_snapshot=kpi_snapshot,
        )
    filename = f"project_{project_id}_report.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


def _build_tree_stats(rows: list[dict]) -> dict:
    total = len(rows)
    alive = sum(1 for r in rows if _normalize_tree_status(r.get("status")) in HEALTHY_TREE_STATUSES)
    dead = sum(1 for r in rows if _normalize_tree_status(r.get("status")) in DEAD_TREE_STATUSES)
    needs_attention = sum(1 for r in rows if _normalize_tree_status(r.get("status")) in ATTENTION_TREE_STATUSES)
    pending = sum(1 for r in rows if _normalize_tree_status(r.get("status")) == "pending_planting")
    survival_rate = round((alive / total) * 100, 1) if total else 0.0
    return {
        "total": total,
        "alive": alive,
        "dead": dead,
        "needs_attention": needs_attention,
        "pending_planting": pending,
        "survival_rate": survival_rate,
    }


@router.get("/work-report/pdf")
def export_work_report_pdf(
    project_id: int,
    assignee_name: str | None = None,
    lng: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    zoom: float | None = Query(default=None),
    bearing: float | None = Query(default=0.0),
    pitch: float | None = Query(default=0.0),
    db: Session = Depends(get_db),
):
    def _coerce_optional_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            if isinstance(value, bool):
                return None
            return float(value)
        except Exception:
            return None

    lng_value = _coerce_optional_float(lng)
    lat_value = _coerce_optional_float(lat)
    zoom_value = _coerce_optional_float(zoom)
    bearing_value = _coerce_optional_float(bearing)
    pitch_value = _coerce_optional_float(pitch)

    project = get_project(project_id, db)
    if assignee_name:
        rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id AND created_by = :assignee_name
            ORDER BY created_at DESC
            LIMIT 200
        """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
        map_rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id AND created_by = :assignee_name
            ORDER BY created_at DESC
            LIMIT 1000
        """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
    else:
        rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id
            ORDER BY created_at DESC
            LIMIT 200
        """), {"project_id": project_id}).mappings().all()
        map_rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id
            ORDER BY created_at DESC
            LIMIT 1000
        """), {"project_id": project_id}).mappings().all()

    maintenance_rows = _maintenance_summary_by_tree(project_id, db, assignee_name)
    rows = _attach_maintenance_to_tree_rows(rows, maintenance_rows)
    map_rows = _attach_maintenance_to_tree_rows(map_rows, maintenance_rows)

    project_copy = dict(project)
    project_copy["stats"] = _build_tree_stats(map_rows)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_work_map_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    map_png = _build_report_map_png(
        map_rows=map_rows,
        lng=lng_value,
        lat=lat_value,
        zoom=zoom_value,
        bearing=bearing_value,
        pitch=pitch_value,
    )

    map_view = None
    if lng_value is not None and lat_value is not None and zoom_value is not None:
        map_view = {"lng": lng_value, "lat": lat_value, "zoom": zoom_value}
    donor_rows = _build_donor_report_rows(project_id, db)
    kpi_snapshot = _compute_kpi_snapshot(project_id, db)
    try:
        render_green_report_pdf(
            pdf_path,
            project_copy,
            rows,
            map_png=map_png,
            map_rows=map_rows,
            map_view=map_view,
            maintenance_rows=maintenance_rows,
            donor_rows=donor_rows,
            kpi_snapshot=kpi_snapshot,
        )
    except Exception:
        render_green_report_pdf(
            pdf_path,
            project_copy,
            rows,
            map_png=None,
            map_rows=map_rows,
            map_view=map_view,
            maintenance_rows=maintenance_rows,
            donor_rows=donor_rows,
            kpi_snapshot=kpi_snapshot,
        )
    filename = (
        f"project_{project_id}_work_report_{assignee_name}.pdf"
        if assignee_name
        else f"project_{project_id}_work_report_all.pdf"
    )
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)
