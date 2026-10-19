from __future__ import annotations

"""Buyer-facing online plot payments (Paystack/Flutterwave) through the Buyer Portal magic-link
token - the online counterpart to the staff-only manual bank-transfer flow in
app/services/estates/payments.py. Dispatches through app/utils/estate_payment_provider.py exactly
like app/services/estates/subscriptions.py does, but the ledger row (EstateAllocationChargeAttempt)
is scoped to an allocation, not a subscription, since a buyer's plot payment has nothing to do
with the org's own LandCheck subscription billing."""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.estate_foundation import (
    Estate,
    EstateAllocation,
    EstateAllocationChargeAttempt,
    EstateCustomer,
    EstateOrganization,
    EstatePlot,
)
from app.services.estates import estate_email
from app.services.estates.allocations import reserve_or_allocate
from app.services.estates.authorization import EstatePrincipal
from app.services.estates.commissions import apply_commission
from app.services.estates.operations import advance_payment_schedule_after_payment, issue_customer_portal_link, record_notification_log
from app.services.estates.payments import confirm_payment, financial_summary, record_payment
from app.services.estates.payout_accounts import get_payout_account
from app.utils.estate_payment_provider import module_for

logger = logging.getLogger(__name__)

_ONLINE_PAYMENT_ACTOR = EstatePrincipal("system", "online_payment", "Online payment")


def new_tx_ref() -> str:
    return f"ALLOC-{uuid.uuid4().hex[:12].upper()}"


def initiate_allocation_payment(
    db: Session,
    *,
    allocation: EstateAllocation,
    amount: Decimal,
    customer_email: str,
    customer_name: str,
    organization_name: str,
    return_url: str,
    portal_token: str,
) -> dict:
    """Validates the requested amount against the outstanding balance, then opens a hosted
    Paystack/Flutterwave checkout. `portal_token` is stashed on the ledger row's provider_payload
    so the return/webhook handlers can send the buyer back to their own portal."""
    if allocation.status not in {"reserved", "allocated"}:
        raise ValueError("This plot is not in an active allocation")
    payout_account = get_payout_account(db, allocation.organization_id)
    if not payout_account:
        # Never let a buyer payment fall back to settling into LandCheck's own account - online
        # payment simply isn't offered until the developer links their bank account.
        raise ValueError("This developer has not set up online payments yet")
    summary = financial_summary(db, allocation)
    if summary.outstanding <= 0:
        raise ValueError("This allocation has no outstanding balance")
    amount = Decimal(str(amount)) if amount else summary.outstanding
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    if amount > summary.outstanding + Decimal("0.01"):
        raise ValueError("Payment amount exceeds the outstanding balance")

    if payout_account.paystack_subaccount_code:
        provider = "paystack"
        split_kwargs = {"subaccount_code": payout_account.paystack_subaccount_code}
    elif payout_account.flutterwave_subaccount_id:
        provider = "flutterwave"
        split_kwargs = {"subaccount_id": payout_account.flutterwave_subaccount_id}
    else:
        raise ValueError("This developer's payout account is not linked to a payment gateway")

    existing_pending = (
        db.query(EstateAllocationChargeAttempt)
        .filter(EstateAllocationChargeAttempt.allocation_id == allocation.id, EstateAllocationChargeAttempt.status == "pending")
        .order_by(EstateAllocationChargeAttempt.id.desc())
        .first()
    )
    if existing_pending:
        existing_pending.status = "failed"
        existing_pending.failure_reason = "Replaced by a new buyer-initiated checkout."

    tx_ref = new_tx_ref()
    attempt = EstateAllocationChargeAttempt(
        organization_id=allocation.organization_id,
        allocation_id=allocation.id,
        customer_id=allocation.customer_id,
        amount=amount,
        currency="NGN",
        payment_provider=provider,
        tx_ref=tx_ref,
        status="pending",
    )
    db.add(attempt)
    db.flush()
    try:
        checkout_url = module_for(provider).initiate_checkout(
            tx_ref=tx_ref,
            amount=amount,
            currency="NGN",
            email=customer_email,
            name=customer_name,
            redirect_url=return_url,
            title=organization_name,
            description=f"Plot payment for {organization_name}",
            payment_options="card,banktransfer",
            **split_kwargs,
            meta={
                "purpose": "estate_allocation_payment",
                "allocation_id": str(allocation.id),
                "charge_attempt_id": str(attempt.id),
                "customer_email": customer_email,
            },
        )
    except Exception:
        db.rollback()
        raise
    attempt.provider_payload = {"checkout_url": checkout_url, "portal_token": portal_token}
    db.commit()
    return {"checkout_url": checkout_url, "tx_ref": tx_ref}


def find_charge_attempt(db: Session, verify_data: dict, tx_ref: str | None = None) -> EstateAllocationChargeAttempt | None:
    reference = str(tx_ref or verify_data.get("tx_ref") or "").strip()
    transaction_id = str(verify_data.get("id") or "").strip()
    filters = []
    if reference:
        filters.append(EstateAllocationChargeAttempt.tx_ref == reference)
    if transaction_id:
        filters.append(EstateAllocationChargeAttempt.provider_transaction_id == transaction_id)
    if not filters:
        return None
    return db.query(EstateAllocationChargeAttempt).filter(or_(*filters)).with_for_update().first()


def _notify_buyer_payment(db: Session, *, allocation: EstateAllocation, organization_name: str, amount_just_paid: Decimal) -> None:
    customer = db.get(EstateCustomer, allocation.customer_id)
    plot = db.get(EstatePlot, allocation.plot_id)
    estate = db.get(Estate, allocation.estate_id)
    if not customer or not plot or not estate:
        return
    if not allocation.share_token:
        allocation.share_token = uuid.uuid4().hex
    summary = financial_summary(db, allocation)
    portal_url = None
    if customer.email:
        _, portal_url = issue_customer_portal_link(db, customer=customer, actor=_ONLINE_PAYMENT_ACTOR)
    notified = bool(customer.email) and estate_email.notify_customer(
        to_email=customer.email,
        customer_name=customer.full_name,
        org_name=organization_name,
        estate_name=estate.name,
        plot_number=plot.plot_number,
        event="payment_recorded",
        agreed_price=summary.agreed_price,
        confirmed_paid=summary.confirmed_paid,
        outstanding=summary.outstanding,
        amount_just_paid=amount_just_paid,
        share_token=allocation.share_token,
        portal_url=portal_url,
        payment_due_at=allocation.next_payment_due_at,
    )
    record_notification_log(
        db,
        organization_id=allocation.organization_id,
        estate_id=allocation.estate_id,
        customer_id=allocation.customer_id,
        allocation_id=allocation.id,
        event_key="payment_recorded",
        recipient_email=customer.email,
        recipient_name=customer.full_name,
        subject=f"Payment received - {estate.name}",
        status="sent" if notified else ("failed" if customer.email else "skipped"),
    )


def complete_allocation_payment(db: Session, verify_data: dict, tx_ref: str | None = None) -> dict:
    """Mirrors app/routers/estate_billing.py's _complete_subscription_payment: locks the ledger
    row, re-checks the gateway's own verified amount/currency/status (never the caller's claim),
    is idempotent on an already-success row, and on success applies the exact same domain effect
    staff already apply by hand when confirming a manual payment - record_payment() then
    confirm_payment(), followed by the same auto-allocate-on-fully-paid + commission logic the
    staff confirm route runs."""
    attempt = find_charge_attempt(db, verify_data, tx_ref)
    if not attempt:
        return {"ok": False, "message": "Could not match this payment to an allocation."}
    if attempt.status == "success":
        return {"ok": True, "already_completed": True}

    allocation = db.query(EstateAllocation).filter(EstateAllocation.id == attempt.allocation_id).with_for_update().one_or_none()
    if not allocation:
        attempt.status = "failed"
        attempt.failure_reason = "Allocation no longer exists."
        db.commit()
        return {"ok": False, "message": "Allocation no longer exists."}

    status = str(verify_data.get("status") or "").lower()
    amount = Decimal(str(verify_data.get("charged_amount") or verify_data.get("amount") or "0"))
    currency = str(verify_data.get("currency") or "").upper()
    payload = dict(attempt.provider_payload or {}) if isinstance(attempt.provider_payload, dict) else {}
    attempt.provider_transaction_id = str(verify_data.get("id") or "") or attempt.provider_transaction_id
    attempt.provider_payload = {**payload, "provider": verify_data or payload.get("provider")}
    if status in {"pending", "processing", "awaiting_authorization", "queued"}:
        attempt.failure_reason = "Payment authorization is pending."
        db.commit()
        return {"ok": False, "pending": True}
    if status not in {"successful", "succeeded"} or currency != "NGN" or amount + Decimal("0.01") < Decimal(str(attempt.amount)):
        attempt.status = "failed"
        attempt.failure_reason = str(verify_data.get("processor_response") or status or "Payment was not successful")
        db.commit()
        return {"ok": False, "message": "Payment was not successful."}

    organization = db.get(EstateOrganization, attempt.organization_id)
    payment = record_payment(
        db,
        allocation=allocation,
        amount=Decimal(str(attempt.amount)),
        payment_date=datetime.now(timezone.utc),
        method=f"online_{attempt.payment_provider}",
        reference=attempt.tx_ref,
        notes="Paid online by the buyer through the Buyer Portal.",
        actor=_ONLINE_PAYMENT_ACTOR,
        confirmation_required=False,
    )
    confirm_payment(db, payment=payment, actor=_ONLINE_PAYMENT_ACTOR)
    advance_payment_schedule_after_payment(db, allocation=allocation, amount=payment.amount)
    attempt.status = "success"
    attempt.failure_reason = None
    attempt.resulting_payment_id = payment.id
    attempt.completed_at = datetime.now(timezone.utc)

    summary = financial_summary(db, allocation)
    fully_paid = summary.agreed_price > 0 and summary.outstanding <= 0
    if fully_paid and allocation.status == "reserved":
        plot = db.get(EstatePlot, allocation.plot_id)
        customer = db.get(EstateCustomer, allocation.customer_id)
        if plot and customer:
            reserve_or_allocate(db, plot=plot, customer=customer, actor=_ONLINE_PAYMENT_ACTOR, allocate=True)
            apply_commission(db, allocation=allocation)

    if organization:
        _notify_buyer_payment(db, allocation=allocation, organization_name=organization.name, amount_just_paid=payment.amount)
    db.commit()
    return {"ok": True, "payment_id": payment.id}
