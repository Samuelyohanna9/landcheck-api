from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from geoalchemy2.shape import to_shape
from sqlalchemy import func, or_, tuple_
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateAllocation,
    EstateAuditEvent,
    EstateCustomer,
    EstateCustomerPortalToken,
    EstateCommissionPayout,
    EstateDocument,
    EstateDocumentLink,
    EstatePayment,
    EstatePaymentInbox,
    EstateOrganization,
    EstatePlot,
    EstateStakingTask,
    EstateSurveyRequest,
)
from app.services.estates.allocations import release_allocation
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal
from app.services.estates.payments import financial_summary
from app.services.estates import estate_email


DOCUMENT_REQUIREMENTS = (
    {"stage": "reservation", "type": "proof_of_identity", "label": "Proof of identity", "required": True},
    {"stage": "allocation", "type": "allocation_letter", "label": "Allocation letter", "required": True},
    {"stage": "survey", "type": "survey_plan", "label": "Survey plan", "required": True},
    {"stage": "staking", "type": "staking_evidence", "label": "Staking evidence", "required": True},
    {"stage": "handover", "type": "handover_pack", "label": "Handover pack", "required": True},
)

PUBLIC_DOCUMENT_TYPES = {"allocation_letter", "survey_plan", "title_document", "receipt", "handover_pack"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_portal_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def issue_customer_portal_token(
    db: Session,
    *,
    customer: EstateCustomer,
    actor: EstatePrincipal,
    expires_in_days: int = 90,
) -> tuple[EstateCustomerPortalToken, str]:
    raw_token = secrets.token_urlsafe(36)
    row = EstateCustomerPortalToken(
        organization_id=customer.organization_id,
        customer_id=customer.id,
        token_hash=hash_portal_token(raw_token),
        expires_at=_now() + timedelta(days=max(1, min(int(expires_in_days), 365))),
        created_by_subject_type=actor.subject_type,
        created_by_subject_id=actor.subject_id,
    )
    db.add(row)
    db.flush()
    append_estate_audit_event(
        db,
        organization_id=customer.organization_id,
        actor=actor,
        action="customer.portal_token.created",
        entity_type="estate_customer_portal_token",
        entity_id=row.id,
        after_data={"customer_id": customer.id, "expires_at": row.expires_at.isoformat()},
    )
    return row, raw_token


def linked_documents(db: Session, allocation: EstateAllocation) -> list[EstateDocument]:
    entity_pairs = [("allocation", str(allocation.id)), ("customer", str(allocation.customer_id)), ("plot", str(allocation.plot_id))]
    rows = (
        db.query(EstateDocument)
        .join(EstateDocumentLink, EstateDocumentLink.document_id == EstateDocument.id)
        .filter(
            EstateDocument.organization_id == allocation.organization_id,
            tuple_(EstateDocumentLink.entity_type, EstateDocumentLink.entity_id).in_(entity_pairs),
        )
        .all()
    )
    return rows


def document_readiness(db: Session, allocation: EstateAllocation) -> dict:
    # Avoid a tuple-IN dependency on every supported database by using one small OR clause.
    links = db.query(EstateDocumentLink).filter(or_(
        (EstateDocumentLink.entity_type == "allocation") & (EstateDocumentLink.entity_id == str(allocation.id)),
        (EstateDocumentLink.entity_type == "customer") & (EstateDocumentLink.entity_id == str(allocation.customer_id)),
        (EstateDocumentLink.entity_type == "plot") & (EstateDocumentLink.entity_id == str(allocation.plot_id)),
    )).all()
    document_ids = {link.document_id for link in links}
    types = {
        document.document_type
        for document in db.query(EstateDocument).filter(EstateDocument.id.in_(document_ids or {-1})).all()
    }
    stages = []
    for requirement in DOCUMENT_REQUIREMENTS:
        stages.append({**requirement, "status": "ready" if requirement["type"] in types else "missing"})
    return {
        "allocation_id": allocation.id,
        "complete": all(item["status"] == "ready" for item in stages if item["required"]),
        "stages": stages,
        "linked_document_count": len(document_ids),
    }


def _payment_plan_items(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("installments") or parsed.get("stages") or []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _has_overdue_installment(allocation: EstateAllocation, summary, now: datetime) -> bool:
    if summary.outstanding <= 0:
        return False
    if allocation.next_payment_due_at:
        due_at = allocation.next_payment_due_at if allocation.next_payment_due_at.tzinfo else allocation.next_payment_due_at.replace(tzinfo=timezone.utc)
        if due_at < now:
            return True
    for item in _payment_plan_items(allocation.payment_plan):
        due = item.get("due_date") or item.get("due_at")
        if not due:
            continue
        try:
            due_at = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            if due_at < now:
                return True
        except ValueError:
            continue
    return False


def build_operations_summary(db: Session, estate: Estate) -> dict:
    now = _now()
    allocations = db.query(EstateAllocation).filter(EstateAllocation.estate_id == estate.id).all()
    active_allocations = [row for row in allocations if row.status in {"reserved", "allocated"}]
    surveys = {row.plot_id: row for row in db.query(EstateSurveyRequest).filter(EstateSurveyRequest.estate_id == estate.id).all()}
    staking = {row.plot_id: row for row in db.query(EstateStakingTask).filter(EstateStakingTask.estate_id == estate.id).all()}
    summaries = {row.id: financial_summary(db, row) for row in active_allocations}
    missing_documents = sum(1 for row in active_allocations if not document_readiness(db, row)["complete"])
    payment_overdue = sum(1 for row in active_allocations if _has_overdue_installment(row, summaries[row.id], now))
    expiring = sum(
        1
        for row in active_allocations
        if row.status == "reserved" and row.reservation_expires_at and now <= row.reservation_expires_at <= now + timedelta(hours=72)
    )
    allocated_not_surveyed = sum(
        1
        for row in active_allocations
        if row.status == "allocated" and (not surveys.get(row.plot_id) or surveys[row.plot_id].status not in {"completed", "approved"})
    )
    survey_not_staked = sum(
        1
        for row in surveys.values()
        if row.status in {"completed", "approved"} and (not staking.get(row.plot_id) or staking[row.plot_id].status not in {"completed", "approved"})
    )
    commission_paid = {
        allocation_id: Decimal(str(total or 0))
        for allocation_id, total in db.query(
            EstateCommissionPayout.allocation_id,
            func.coalesce(func.sum(EstateCommissionPayout.amount), 0),
        ).filter(EstateCommissionPayout.allocation_id.in_([row.id for row in active_allocations] or [-1])).group_by(EstateCommissionPayout.allocation_id).all()
    }
    commission_pending = sum(
        1 for row in active_allocations
        if row.status == "allocated"
        and row.commission_amount
        and Decimal(str(row.commission_amount)) > commission_paid.get(row.id, Decimal("0"))
    )
    unmatched_payments = db.query(EstatePaymentInbox).filter(EstatePaymentInbox.organization_id == estate.organization_id, EstatePaymentInbox.status == "unmatched").count()
    latest_plot_update = db.query(func.max(EstatePlot.updated_at)).filter(EstatePlot.estate_id == estate.id).scalar()
    latest_public_update = db.query(func.max(EstateAuditEvent.created_at)).filter(
        EstateAuditEvent.organization_id == estate.organization_id,
        EstateAuditEvent.entity_type == "estate",
        EstateAuditEvent.entity_id == str(estate.id),
        EstateAuditEvent.action.in_(("estate.public_settings.updated", "public_page.published", "estate.public_logo_updated")),
    ).scalar()
    public_outdated = bool(estate.public_enabled and latest_plot_update and (not latest_public_update or latest_plot_update > latest_public_update))

    actions = [
        {"key": "payment_overdue", "label": "Payments overdue", "count": payment_overdue, "href": "/estates/payments", "tone": "danger"},
        {"key": "reservation_expiring", "label": "Reservations expiring", "count": expiring, "href": f"/estates/{estate.id}/customers", "tone": "warn"},
        {"key": "documents_missing", "label": "Documents missing", "count": missing_documents, "href": "/estates/documents", "tone": "warn"},
        {"key": "allocated_not_surveyed", "label": "Allocated but not surveyed", "count": allocated_not_surveyed, "href": f"/estates/{estate.id}/survey", "tone": "info"},
        {"key": "survey_not_staked", "label": "Survey completed but not staked", "count": survey_not_staked, "href": f"/estates/{estate.id}/staking", "tone": "info"},
        {"key": "commission_pending", "label": "Commission awaiting approval", "count": commission_pending, "href": "/estates/commissions", "tone": "warn"},
        {"key": "public_outdated", "label": "Public page data outdated", "count": 1 if public_outdated else 0, "href": f"/estates/{estate.id}/settings", "tone": "info"},
        {"key": "unmatched_payments", "label": "Unmatched payments", "count": unmatched_payments, "href": "/estates/reconciliation", "tone": "danger"},
    ]
    return {
        "estate_id": estate.id,
        "generated_at": now,
        "counts": {item["key"]: item["count"] for item in actions},
        "actions": [item for item in actions if item["count"]],
        "total_exceptions": sum(item["count"] for item in actions),
    }


def expire_due_reservations(db: Session) -> int:
    now = _now()
    actor = EstatePrincipal("system", "reservation-expiry", "Reservation expiry")
    reminder_rows = db.query(EstateAllocation).filter(
        EstateAllocation.status == "reserved",
        EstateAllocation.reservation_expires_at.isnot(None),
        EstateAllocation.reservation_expires_at > now,
        EstateAllocation.reservation_expires_at <= now + timedelta(hours=24),
        EstateAllocation.reservation_reminder_sent_at.is_(None),
    ).with_for_update(skip_locked=True).all()
    for row in reminder_rows:
        customer = db.get(EstateCustomer, row.customer_id)
        estate = db.get(Estate, row.estate_id)
        plot = db.get(EstatePlot, row.plot_id)
        if not customer or not customer.email or not estate or not plot:
            continue
        organization = db.get(EstateOrganization, row.organization_id)
        if estate_email.notify_customer(to_email=customer.email, customer_name=customer.full_name, org_name=organization.name if organization else "Estate team", estate_name=estate.name, plot_number=plot.plot_number, event="reservation_expiring", share_token=row.share_token):
            row.reservation_reminder_sent_at = now
    rows = db.query(EstateAllocation).filter(
        EstateAllocation.status == "reserved",
        EstateAllocation.reservation_expires_at.isnot(None),
        EstateAllocation.reservation_expires_at <= now,
    ).with_for_update(skip_locked=True).all()
    count = 0
    for row in rows:
        try:
            release_allocation(db, allocation=row, actor=actor, reason="Reservation expired automatically")
            count += 1
        except Exception:
            continue
    return count


def buyer_portal_payload(db: Session, token_row: EstateCustomerPortalToken) -> dict:
    now = _now()
    token_row.last_used_at = now
    customer = db.get(EstateCustomer, token_row.customer_id)
    if not customer:
        return {"customer": None, "allocations": []}
    allocations = db.query(EstateAllocation).filter(
        EstateAllocation.customer_id == customer.id,
        EstateAllocation.organization_id == customer.organization_id,
        EstateAllocation.status.in_(("reserved", "allocated")),
    ).order_by(EstateAllocation.created_at.desc()).all()
    payload = []
    for allocation in allocations:
        estate = db.get(Estate, allocation.estate_id)
        plot = db.get(EstatePlot, allocation.plot_id)
        summary = financial_summary(db, allocation)
        docs = linked_documents(db, allocation)
        survey = db.query(EstateSurveyRequest).filter(EstateSurveyRequest.allocation_id == allocation.id).order_by(EstateSurveyRequest.created_at.desc()).first()
        payload.append({
            "allocation_id": allocation.id,
            "status": allocation.status,
            "estate": {"id": estate.id, "name": estate.name} if estate else None,
            "plot": {"id": plot.id, "number": plot.plot_number, "area_sqm": float(plot.area_sqm or 0), "geometry": __import__("shapely.geometry", fromlist=["mapping"]).mapping(to_shape(plot.geometry)) if plot and plot.geometry else None},
            "financial": {"agreed_price": str(summary.agreed_price), "confirmed_paid": str(summary.confirmed_paid), "outstanding": str(summary.outstanding), "percentage": str(summary.percentage)},
            "reservation_expires_at": allocation.reservation_expires_at,
            "survey_status": survey.status if survey else "not_started",
            "document_readiness": document_readiness(db, allocation),
            "documents": [{"id": doc.id, "filename": doc.original_filename, "type": doc.document_type} for doc in docs if doc.document_type in PUBLIC_DOCUMENT_TYPES],
            "payments": [{"date": payment.payment_date, "amount": str(payment.amount), "status": payment.status, "receipt_number": payment.receipt_number, "reference": payment.reference_no} for payment in db.query(EstatePayment).filter(EstatePayment.allocation_id == allocation.id).order_by(EstatePayment.payment_date.desc()).all()],
        })
    return {"customer": {"name": customer.full_name, "reference": customer.reference_no}, "allocations": payload, "generated_at": now}
