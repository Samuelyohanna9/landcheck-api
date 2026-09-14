from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateAuditEvent
from app.services.estates.authorization import EstatePrincipal


def append_estate_audit_event(
    db: Session,
    *,
    organization_id: int,
    actor: EstatePrincipal | None,
    action: str,
    entity_type: str,
    entity_id: str | int,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> EstateAuditEvent:
    """Append one immutable event in the caller's transaction.

    This deliberately does not commit. A future critical action can atomically persist its change
    and audit event, while the migration prevents update/delete mutations after insertion.
    """
    clean_action = str(action or "").strip()
    clean_entity_type = str(entity_type or "").strip()
    clean_entity_id = str(entity_id or "").strip()
    if not clean_action or not clean_entity_type or not clean_entity_id:
        raise ValueError("Estate audit action and entity are required")
    event = EstateAuditEvent(
        organization_id=int(organization_id),
        actor_subject_type=actor.subject_type if actor else None,
        actor_subject_id=actor.subject_id if actor else None,
        action=clean_action[:120],
        entity_type=clean_entity_type[:80],
        entity_id=clean_entity_id[:128],
        before_data=deepcopy(before_data) if before_data is not None else None,
        after_data=deepcopy(after_data) if after_data is not None else None,
        metadata_json=deepcopy(metadata) if metadata is not None else {},
        correlation_id=str(correlation_id or "").strip()[:128] or None,
    )
    db.add(event)
    db.flush()
    return event
