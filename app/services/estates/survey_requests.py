"""Estate Survey request lifecycle rules."""
from __future__ import annotations

from fastapi import HTTPException

ACTIVE_SURVEY_REQUEST_STATUSES = frozenset({"requested", "assigned", "in_progress", "ready_for_review", "approved", "failed"})
TRANSITIONS = {
    "requested": frozenset({"assigned", "in_progress", "cancelled"}),
    "assigned": frozenset({"in_progress", "cancelled"}),
    "in_progress": frozenset({"ready_for_review", "approved", "completed", "failed"}),
    "ready_for_review": frozenset({"approved", "completed", "failed"}),
    "approved": frozenset({"completed"}),
}

def transition(request, target: str) -> None:
    target = target.lower()
    if target not in TRANSITIONS.get(request.status, frozenset()):
        raise HTTPException(409, "Survey request transition is not allowed")
    request.status = target
