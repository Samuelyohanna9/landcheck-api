from __future__ import annotations

"""Data Processing Agreement acceptance for Estate companies (see docs/legal/)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.routers.plots import get_db
from app.models.estate_auth import EstateAccount
from app.services.estates import dpa
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import require_estate_access
from app.services.estates.marketing_common import client_ip

router = APIRouter(prefix="/estates/legal", tags=["estate-legal"])


def _acceptance_payload(row) -> dict | None:
    if row is None:
        return None
    return {
        "version": row.version,
        "accepted_at": row.accepted_at,
        "accepted_by_name": row.accepted_by_name,
        "accepted_by_email": row.accepted_by_email,
    }


@router.get("/dpa")
def dpa_status(organization_id: int, request: Request, db: Session = Depends(get_db)):
    """Any staff member can view the agreement and its acceptance status."""
    require_estate_access(db, request, organization_id, require_subscription=False)
    latest = dpa.latest_acceptance(db, organization_id)
    return {
        "current_version": dpa.DPA_VERSION,
        "accepted": bool(latest and latest.version == dpa.DPA_VERSION),
        "latest_acceptance": _acceptance_payload(latest),
    }


@router.post("/dpa/accept")
def dpa_accept(organization_id: int, request: Request, db: Session = Depends(get_db)):
    """Committing the company to the agreement is an owner-level decision, like billing."""
    access = require_estate_access(db, request, organization_id, permission="billing.manage", require_subscription=False)
    principal = access.principal
    email = principal.display_name if "@" in (principal.display_name or "") else None
    if principal.subject_type == "estate_account":
        try:
            account = db.get(EstateAccount, int(principal.subject_id))
        except (TypeError, ValueError):
            account = None
        if account is not None:
            email = account.email
    row = dpa.record_acceptance(
        db,
        organization_id=organization_id,
        subject_type=principal.subject_type,
        subject_id=principal.subject_id,
        name=principal.display_name,
        email=email,
        ip_address=client_ip(request),
    )
    append_estate_audit_event(
        db, organization_id=organization_id, actor=principal, action="dpa.accepted",
        entity_type="estate_organization", entity_id=organization_id, after_data={"version": row.version},
    )
    db.commit()
    return {"current_version": dpa.DPA_VERSION, "accepted": True, "latest_acceptance": _acceptance_payload(row)}
