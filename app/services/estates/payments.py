from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.estate_foundation import EstateAllocation, EstatePayment
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal

@dataclass(frozen=True, slots=True)
class FinancialSummary:
    agreed_price: Decimal
    confirmed_paid: Decimal
    pending_paid: Decimal
    outstanding: Decimal
    percentage: Decimal

def financial_summary(db: Session, allocation: EstateAllocation) -> FinancialSummary:
    confirmed = db.query(func.coalesce(func.sum(EstatePayment.amount), 0)).filter(EstatePayment.allocation_id == allocation.id, EstatePayment.status == "confirmed").scalar()
    pending = db.query(func.coalesce(func.sum(EstatePayment.amount), 0)).filter(EstatePayment.allocation_id == allocation.id, EstatePayment.status.in_(("recorded","pending_confirmation"))).scalar()
    price = Decimal(allocation.agreed_price or 0); confirmed = Decimal(confirmed or 0); pending = Decimal(pending or 0)
    outstanding = max(Decimal(0), price - confirmed)
    return FinancialSummary(price, confirmed, pending, outstanding, (confirmed / price * 100 if price else Decimal(0)))

def record_payment(db: Session, *, allocation: EstateAllocation, amount: Decimal, payment_date: datetime, method: str, reference: str | None, notes: str | None, actor: EstatePrincipal, confirmation_required: bool = True, idempotency_key: str | None = None) -> EstatePayment:
    if allocation.status not in {"reserved", "allocated"}: raise HTTPException(409, "Payments require an active allocation")
    if amount <= 0: raise HTTPException(422, "Payment amount must be positive")
    summary = financial_summary(db, allocation)
    if allocation.agreed_price and summary.confirmed_paid + amount > summary.agreed_price: raise HTTPException(409, "Payment exceeds the outstanding balance")
    payment = EstatePayment(organization_id=allocation.organization_id, allocation_id=allocation.id, customer_id=allocation.customer_id, plot_id=allocation.plot_id, amount=amount, payment_date=payment_date, payment_method=method, reference_no=reference or None, notes=notes, status="pending_confirmation" if confirmation_required else "recorded", recorded_by_subject_type=actor.subject_type, recorded_by_subject_id=actor.subject_id, idempotency_key=idempotency_key)
    db.add(payment); db.flush()
    payment.receipt_number = f"LC-{payment.payment_uid[:8].upper()}"
    append_estate_audit_event(db, organization_id=allocation.organization_id, actor=actor, action="payment.recorded", entity_type="estate_payment", entity_id=payment.id, after_data={"amount": str(amount), "status": payment.status, "reference": reference, "receipt_number": payment.receipt_number})
    return payment

def confirm_payment(db: Session, *, payment: EstatePayment, actor: EstatePrincipal) -> EstatePayment:
    if payment.status == "confirmed":
        raise HTTPException(409, "Payment is already confirmed")
    if payment.status not in {"recorded", "pending_confirmation"}: raise HTTPException(409, "Payment cannot be confirmed")
    payment.status="confirmed"; payment.confirmed_at=datetime.now(timezone.utc); payment.confirmed_by_subject_type=actor.subject_type; payment.confirmed_by_subject_id=actor.subject_id
    if not payment.receipt_number:
        payment.receipt_number = f"LC-{payment.payment_uid[:8].upper()}"
    append_estate_audit_event(db, organization_id=payment.organization_id, actor=actor, action="payment.confirmed", entity_type="estate_payment", entity_id=payment.id, before_data={"status":"pending_confirmation"}, after_data={"status":"confirmed"})
    return payment

def void_payment(db: Session, *, payment: EstatePayment, actor: EstatePrincipal, reason: str) -> EstatePayment:
    if payment.status in {"voided", "reversed"}: raise HTTPException(409, "Payment is already voided")
    payment.status="voided"; payment.voided_at=datetime.now(timezone.utc); payment.voided_by_subject_type=actor.subject_type; payment.voided_by_subject_id=actor.subject_id; payment.void_reason=reason
    append_estate_audit_event(db, organization_id=payment.organization_id, actor=actor, action="payment.voided", entity_type="estate_payment", entity_id=payment.id, before_data={"status":"confirmed"}, after_data={"status":"voided","reason":reason})
    return payment
