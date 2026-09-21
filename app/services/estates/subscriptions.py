from __future__ import annotations

"""LandCheck Estates subscription lifecycle - trial start, recurring charges, dunning retries,
cancellation, and plan changes. See app/utils/estate_flutterwave.py for the payment plumbing and
app/main.py's `_run_estate_subscription_billing_job` for the daily driver of this module."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.estate_billing import EstateSubscription, EstateSubscriptionCharge
from app.models.estate_foundation import EstateOrganization
from app.services.estates.billing_plans import ACTIVE_SUBSCRIPTION_STATUSES, TRIAL_DAYS, plan_amount, plan_includes_hazard_analysis
from app.services.estates import estate_email
from app.utils import estate_flutterwave as flw

logger = logging.getLogger(__name__)

# Dunning backoff after a failed renewal/trial-conversion charge, in days from the failed attempt.
RETRY_BACKOFF_DAYS = [1, 2, 3]
MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_DAYS)


def new_tx_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def get_subscription(db: Session, organization_id: int) -> EstateSubscription | None:
    return db.query(EstateSubscription).filter(EstateSubscription.organization_id == int(organization_id)).one_or_none()


def is_access_active(subscription: EstateSubscription | None) -> bool:
    return bool(subscription and subscription.status in ACTIVE_SUBSCRIPTION_STATUSES)


def has_hazard_access(subscription: EstateSubscription | None) -> bool:
    return is_access_active(subscription) and plan_includes_hazard_analysis(subscription.plan_key)


def _advance_period(start: datetime, billing_cycle: str) -> datetime:
    return start + (relativedelta(months=1) if billing_cycle == "monthly" else relativedelta(years=1))


def start_trial(
    db: Session,
    *,
    organization: EstateOrganization,
    plan_key: str,
    billing_cycle: str,
    email: str,
    card_token: str | None,
    card_last4: str | None,
    card_brand: str | None,
) -> EstateSubscription:
    """Called after the verification checkout has been refunded; this is a single-use trial."""
    if not card_token:
        raise ValueError("A reusable card token is required to start the free trial")
    now = datetime.now(timezone.utc)
    trial_ends_at = now + timedelta(days=TRIAL_DAYS)
    subscription = get_subscription(db, organization.id)
    if subscription and subscription.trial_ends_at is not None:
        raise ValueError("This organization has already used its free trial")
    if subscription is None:
        subscription = EstateSubscription(organization_id=organization.id)
        db.add(subscription)
    subscription.plan_key = plan_key
    subscription.billing_cycle = billing_cycle
    subscription.status = "trialing"
    subscription.amount = plan_amount(plan_key, billing_cycle)
    subscription.currency = "NGN"
    subscription.trial_ends_at = trial_ends_at
    subscription.current_period_end = None
    subscription.next_charge_at = trial_ends_at
    subscription.next_retry_at = None
    subscription.failed_charge_attempts = 0
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    subscription.flutterwave_customer_email = email
    if card_token:
        subscription.card_token = card_token
        subscription.card_last4 = card_last4
        subscription.card_brand = card_brand
    db.flush()
    estate_email.send_trial_started_email(organization=organization, subscription=subscription)
    return subscription


def _record_charge(
    db: Session,
    *,
    subscription: EstateSubscription,
    charge_type: str,
    amount: Decimal,
    status: str,
    tx_ref: str,
    flutterwave_transaction_id: str | None,
    flutterwave_payload: dict | None,
    failure_reason: str | None,
) -> EstateSubscriptionCharge:
    charge = EstateSubscriptionCharge(
        subscription_id=subscription.id,
        organization_id=subscription.organization_id,
        charge_type=charge_type,
        amount=amount,
        currency=subscription.currency,
        status=status,
        tx_ref=tx_ref,
        flutterwave_transaction_id=flutterwave_transaction_id,
        flutterwave_payload=flutterwave_payload,
        failure_reason=failure_reason,
    )
    db.add(charge)
    db.flush()
    return charge


def attempt_charge(db: Session, subscription: EstateSubscription, *, charge_type: str) -> bool:
    """Charge the stored card exactly once for this billing attempt.

    A pending row is created before contacting Flutterwave. If the provider response is lost,
    the row remains pending instead of being retried automatically, which prevents a duplicate
    charge against the same card.
    """
    organization = db.get(EstateOrganization, subscription.organization_id)
    existing_pending = (
        db.query(EstateSubscriptionCharge)
        .filter(
            EstateSubscriptionCharge.subscription_id == subscription.id,
            EstateSubscriptionCharge.charge_type == charge_type,
            EstateSubscriptionCharge.status == "pending",
        )
        .order_by(EstateSubscriptionCharge.id.desc())
        .first()
    )
    if existing_pending:
        _handle_pending_charge(db, subscription, organization=organization)
        return False

    tx_ref = new_tx_ref("SUB")
    charge = _record_charge(
        db, subscription=subscription, charge_type=charge_type, amount=subscription.amount, status="pending",
        tx_ref=tx_ref, flutterwave_transaction_id=None, flutterwave_payload=None, failure_reason=None,
    )
    if not subscription.card_token:
        charge.status = "failed"
        charge.failure_reason = "No card on file - this card was not tokenizable at signup."
        _handle_failed_charge(db, subscription, organization=organization)
        return False

    try:
        result = flw.charge_token(
            token=subscription.card_token,
            amount=subscription.amount,
            currency=subscription.currency,
            email=subscription.flutterwave_customer_email or (organization.contact_email if organization else ""),
            tx_ref=tx_ref,
        )
    except Exception as exc:  # A lost response may mean Flutterwave already accepted the charge.
        logger.exception("Estate subscription charge failed (org=%s, type=%s)", subscription.organization_id, charge_type)
        charge.failure_reason = "Payment provider response was not confirmed. Use the billing page to complete payment."
        charge.flutterwave_payload = {"status": "unknown", "error": type(exc).__name__}
        _handle_pending_charge(db, subscription, organization=organization)
        return False

    provider_status = str(result.get("status") or "").lower()
    charge.flutterwave_transaction_id = str(result.get("id") or "") or None
    charge.flutterwave_payload = result or None
    if provider_status in {"successful", "succeeded"}:
        charge.status = "success"
        charge.failure_reason = None
        _handle_successful_charge(db, subscription, organization=organization)
        return True
    if provider_status in {"pending", "processing", "awaiting_authorization", "queued"}:
        charge.failure_reason = "Payment authorization is pending. Complete payment from the billing page."
        _handle_pending_charge(db, subscription, organization=organization)
        return False

    charge.status = "failed"
    charge.failure_reason = str(result.get("processor_response") or result.get("status") or "Charge declined")
    _handle_failed_charge(db, subscription, organization=organization)
    return False


def _handle_pending_charge(db: Session, subscription: EstateSubscription, *, organization: EstateOrganization | None) -> None:
    """Hold access in past-due state without scheduling a blind duplicate retry."""
    subscription.status = "past_due"
    subscription.next_retry_at = None
    db.flush()
    if organization:
        estate_email.send_payment_failed_email(organization=organization, subscription=subscription)


def _handle_successful_charge(db: Session, subscription: EstateSubscription, *, organization: EstateOrganization | None) -> None:
    now = datetime.now(timezone.utc)
    period_end = _advance_period(now, subscription.billing_cycle)
    subscription.status = "active"
    subscription.current_period_end = period_end
    subscription.next_charge_at = period_end
    subscription.next_retry_at = None
    subscription.failed_charge_attempts = 0
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    db.flush()
    if organization:
        estate_email.send_payment_receipt_email(organization=organization, subscription=subscription)


def _handle_failed_charge(db: Session, subscription: EstateSubscription, *, organization: EstateOrganization | None) -> None:
    subscription.status = "past_due"
    subscription.failed_charge_attempts = (subscription.failed_charge_attempts or 0) + 1
    now = datetime.now(timezone.utc)
    if subscription.failed_charge_attempts > MAX_RETRY_ATTEMPTS:
        subscription.status = "canceled"
        subscription.canceled_at = now
        subscription.next_retry_at = None
        db.flush()
        if organization:
            estate_email.send_subscription_canceled_email(organization=organization, subscription=subscription, reason="payment_failed")
        return
    backoff_days = RETRY_BACKOFF_DAYS[min(subscription.failed_charge_attempts, len(RETRY_BACKOFF_DAYS)) - 1]
    subscription.next_retry_at = now + timedelta(days=backoff_days)
    db.flush()
    if organization:
        estate_email.send_payment_failed_email(organization=organization, subscription=subscription)


def cancel_subscription(db: Session, subscription: EstateSubscription) -> None:
    organization = db.get(EstateOrganization, subscription.organization_id)
    if subscription.status == "trialing":
        # Nothing has actually been charged yet - cancel takes effect immediately.
        subscription.status = "canceled"
        subscription.canceled_at = datetime.now(timezone.utc)
        subscription.cancel_at_period_end = False
    else:
        # "Cancel anytime" - access continues through the period already paid for; the daily
        # billing job finalizes the cancellation once current_period_end passes.
        subscription.cancel_at_period_end = True
    db.flush()
    if organization:
        estate_email.send_subscription_canceled_email(organization=organization, subscription=subscription, reason="user_requested")


def _apply_plan_change(subscription: EstateSubscription, *, new_plan_key: str) -> None:
    subscription.plan_key = new_plan_key
    subscription.amount = plan_amount(new_plan_key, subscription.billing_cycle)


def finalize_plan_change(
    db: Session,
    subscription: EstateSubscription,
    *,
    new_plan_key: str,
    organization: EstateOrganization | None,
    charged_amount: Decimal,
) -> None:
    """Apply a paid plan change without resetting the already-paid billing period."""
    _apply_plan_change(subscription, new_plan_key=new_plan_key)
    db.flush()
    if organization:
        estate_email.send_plan_change_receipt_email(
            organization=organization,
            subscription=subscription,
            charged_amount=charged_amount,
        )


def change_plan(
    db: Session,
    subscription: EstateSubscription,
    *,
    new_plan_key: str,
    organization: EstateOrganization | None = None,
) -> dict[str, object]:
    """Change a plan and charge the price difference for an active paid upgrade.

    Trial changes are free because the trial has not converted yet. Downgrades take effect
    immediately without a refund; the next renewal uses the lower price. Active upgrades charge
    only the difference for the current billing cycle, while preserving the existing renewal date.
    """
    if new_plan_key == subscription.plan_key:
        return {"changed": False, "payment_status": "not_required", "charged_amount": Decimal("0")}

    current_amount = Decimal(str(subscription.amount))
    new_amount = plan_amount(new_plan_key, subscription.billing_cycle)
    difference = new_amount - current_amount

    if subscription.status == "trialing" or difference <= 0:
        _apply_plan_change(subscription, new_plan_key=new_plan_key)
        db.flush()
        return {"changed": True, "payment_status": "not_required", "charged_amount": Decimal("0")}

    organization = organization or db.get(EstateOrganization, subscription.organization_id)
    if not subscription.card_token:
        raise ValueError("A saved payment card is required to upgrade this active subscription.")

    existing_pending = (
        db.query(EstateSubscriptionCharge)
        .filter(
            EstateSubscriptionCharge.subscription_id == subscription.id,
            EstateSubscriptionCharge.charge_type == "plan_change",
            EstateSubscriptionCharge.status == "pending",
        )
        .order_by(EstateSubscriptionCharge.id.desc())
        .first()
    )
    if existing_pending:
        return {
            "changed": False,
            "payment_status": "pending",
            "charged_amount": Decimal(str(existing_pending.amount)),
        }

    tx_ref = new_tx_ref("SUB")
    charge = _record_charge(
        db,
        subscription=subscription,
        charge_type="plan_change",
        amount=difference,
        status="pending",
        tx_ref=tx_ref,
        flutterwave_transaction_id=None,
        flutterwave_payload={
            "plan_key": new_plan_key,
            "previous_plan_key": subscription.plan_key,
        },
        failure_reason=None,
    )
    email = subscription.flutterwave_customer_email or (organization.contact_email if organization else "")
    try:
        result = flw.charge_token(
            token=subscription.card_token,
            amount=difference,
            currency=subscription.currency,
            email=email,
            tx_ref=tx_ref,
        )
    except Exception as exc:  # A lost response may mean the provider accepted the charge.
        logger.exception("Estate plan-change charge failed (org=%s)", subscription.organization_id)
        charge.failure_reason = "Payment provider response was not confirmed. Billing will not retry automatically."
        charge.flutterwave_payload = {
            "plan_key": new_plan_key,
            "previous_plan_key": subscription.plan_key,
            "provider": {"status": "unknown", "error": type(exc).__name__},
        }
        db.flush()
        return {"changed": False, "payment_status": "pending", "charged_amount": difference}

    provider_status = str(result.get("status") or "").lower()
    charge.flutterwave_transaction_id = str(result.get("id") or "") or None
    charge.flutterwave_payload = {
        "plan_key": new_plan_key,
        "previous_plan_key": subscription.plan_key,
        "provider": result or None,
    }
    if provider_status in {"successful", "succeeded"}:
        charge.status = "success"
        charge.failure_reason = None
        finalize_plan_change(
            db,
            subscription,
            new_plan_key=new_plan_key,
            organization=organization,
            charged_amount=difference,
        )
        return {"changed": True, "payment_status": "success", "charged_amount": difference}
    if provider_status in {"pending", "processing", "awaiting_authorization", "queued"}:
        charge.failure_reason = "Payment authorization is pending. The plan will change when payment completes."
        db.flush()
        return {"changed": False, "payment_status": "pending", "charged_amount": difference}

    charge.status = "failed"
    charge.failure_reason = str(result.get("processor_response") or result.get("status") or "Charge declined")
    db.flush()
    return {"changed": False, "payment_status": "failed", "charged_amount": difference}


def process_due_billing(db: Session) -> dict[str, int]:
    """Driven by the daily scheduler job (see app/main.py). Converts due trials, charges due
    renewals, retries due dunning attempts, and finalizes cancellations whose paid period ended."""
    now = datetime.now(timezone.utc)
    counts = {"trial_conversions": 0, "renewals": 0, "retries": 0, "cancellations_finalized": 0}

    trialing_due = (
        db.query(EstateSubscription)
        .filter(EstateSubscription.status == "trialing", EstateSubscription.trial_ends_at <= now)
        .with_for_update()
        .all()
    )
    for subscription in trialing_due:
        # The DB enforces at most one trial_conversion row per subscription (see the
        # ux_estate_subscription_charges_one_trial_conversion index). A savepoint keeps a
        # constraint violation here from aborting the rest of this billing run.
        try:
            with db.begin_nested():
                attempt_charge(db, subscription, charge_type="trial_conversion")
            counts["trial_conversions"] += 1
        except IntegrityError:
            logger.warning("Duplicate trial_conversion charge attempt skipped (org=%s)", subscription.organization_id)

    renewals_due = (
        db.query(EstateSubscription)
        .filter(EstateSubscription.status == "active", EstateSubscription.cancel_at_period_end.is_(False), EstateSubscription.next_charge_at <= now)
        .with_for_update()
        .all()
    )
    for subscription in renewals_due:
        attempt_charge(db, subscription, charge_type="renewal")
        counts["renewals"] += 1

    retries_due = (
        db.query(EstateSubscription)
        .filter(EstateSubscription.status == "past_due", EstateSubscription.next_retry_at <= now)
        .with_for_update()
        .all()
    )
    for subscription in retries_due:
        attempt_charge(db, subscription, charge_type="retry")
        counts["retries"] += 1

    ending_cancellations = (
        db.query(EstateSubscription)
        .filter(EstateSubscription.status == "active", EstateSubscription.cancel_at_period_end.is_(True), EstateSubscription.current_period_end <= now)
        .with_for_update()
        .all()
    )
    for subscription in ending_cancellations:
        subscription.status = "canceled"
        subscription.canceled_at = now
        counts["cancellations_finalized"] += 1
    db.flush()

    return counts
