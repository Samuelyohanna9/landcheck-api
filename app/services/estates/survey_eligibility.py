"""Server-side payment eligibility for Estate Survey work."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.estate_foundation import EstateAllocation, EstatePayment, EstatePaymentRule


def survey_eligibility(db: Session, allocation: EstateAllocation | None) -> dict[str, object]:
    if allocation is None:
        return {"eligible": True, "confirmed_percentage": "0", "required_percentage": "0", "reason": "No allocation is linked"}
    rule = db.query(EstatePaymentRule).filter(EstatePaymentRule.organization_id == allocation.organization_id, EstatePaymentRule.rule_key == "survey_minimum_confirmed_percentage").one_or_none()
    required = Decimal(rule.percentage or 0) if rule and rule.is_enabled else Decimal(0)
    agreed = Decimal(allocation.agreed_price or 0)
    confirmed = Decimal(db.query(func.coalesce(func.sum(EstatePayment.amount), 0)).filter(EstatePayment.allocation_id == allocation.id, EstatePayment.status == "confirmed").scalar() or 0)
    percentage = confirmed / agreed * 100 if agreed > 0 else Decimal(0)
    eligible = percentage >= required
    return {"eligible": eligible, "confirmed_percentage": str(percentage), "required_percentage": str(required), "reason": "Eligible" if eligible else "Confirmed payments are below the organization survey threshold"}
