from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateAllocation, EstateCustomer, EstatePlot
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal


def reserve_or_allocate(db: Session, *, plot: EstatePlot, customer: EstateCustomer, actor: EstatePrincipal, allocate: bool, expires_at: datetime | None = None, agreed_price=None, payment_plan: str | None = None, notes: str | None = None) -> EstateAllocation:
    if plot.geometry_status != "approved":
        raise HTTPException(status_code=409, detail="Only approved plot geometry can be reserved or allocated")
    if plot.commercial_status not in {"available", "reserved" if allocate else "available"}:
        raise HTTPException(status_code=409, detail="Plot is not available for this action")
    active = db.query(EstateAllocation).filter(EstateAllocation.plot_id == plot.id, EstateAllocation.status.in_(("reserved", "allocated"))).one_or_none()
    if active and not (allocate and active.status == "reserved" and active.customer_id == customer.id):
        raise HTTPException(status_code=409, detail="Plot already has an active reservation or allocation")
    now = datetime.now(timezone.utc)
    if allocate and active:
        active.status = "allocated"
        active.allocation_date = now
        active.reservation_expires_at = None
        if agreed_price is not None:
            active.agreed_price = agreed_price
        if payment_plan is not None:
            active.payment_plan = payment_plan
        plot.commercial_status = "allocated"
        append_estate_audit_event(db, organization_id=active.organization_id, actor=actor, action="allocation.allocated", entity_type="estate_allocation", entity_id=active.id, before_data={"status": "reserved"}, after_data={"status": "allocated"})
        return active
    allocation = EstateAllocation(organization_id=customer.organization_id, estate_id=plot.estate_id, plot_id=plot.id, customer_id=customer.id, status="allocated" if allocate else "reserved", reservation_date=None if allocate else now, reservation_expires_at=None if allocate else expires_at, allocation_date=now if allocate else None, agreed_price=agreed_price, payment_plan=payment_plan, notes=notes, created_by_subject_type=actor.subject_type, created_by_subject_id=actor.subject_id, share_token=uuid.uuid4().hex)
    plot.commercial_status = allocation.status
    db.add(allocation)
    db.flush()
    append_estate_audit_event(db, organization_id=customer.organization_id, actor=actor, action=f"allocation.{allocation.status}", entity_type="estate_allocation", entity_id=allocation.id, after_data={"plot_id": plot.id, "customer_id": customer.id, "status": allocation.status})
    return allocation


def release_allocation(db: Session, *, allocation: EstateAllocation, actor: EstatePrincipal, reason: str) -> EstateAllocation:
    if allocation.status not in {"reserved", "allocated"}:
        raise HTTPException(status_code=409, detail="Only active allocations can be released")
    before = allocation.status
    allocation.status = "released"
    allocation.cancelled_at = datetime.now(timezone.utc)
    allocation.cancellation_reason = str(reason or "").strip() or "Released"
    plot = db.get(EstatePlot, allocation.plot_id)
    if plot:
        plot.commercial_status = "available"
    append_estate_audit_event(db, organization_id=allocation.organization_id, actor=actor, action="allocation.released", entity_type="estate_allocation", entity_id=allocation.id, before_data={"status": before}, after_data={"status": "released"})
    return allocation
