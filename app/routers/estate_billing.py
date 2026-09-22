from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.estate_auth import EstateAccount
from app.models.estate_billing import EstateSubscription, EstateSubscriptionCharge
from app.models.estate_foundation import EstateOrganization
from app.routers.plots import get_db
from app.schemas.estate_billing import ChangePlanRequest, ChoosePlanRequest
from app.services.estates.authorization import EstateAccess, require_estate_access
from app.services.estates.billing_plans import ESTATE_PLANS, TRIAL_DAYS, VERIFICATION_CHARGE_AMOUNT
from app.services.estates.subscriptions import (
    _handle_successful_charge,
    cancel_subscription,
    change_plan,
    finalize_plan_change,
    get_subscription,
    new_tx_ref,
    start_trial,
)
from app.utils import estate_flutterwave as flw
from app.utils.auth_security import require_signed_flutterwave_webhooks

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estates/billing", tags=["estate-billing"])


def _api_public_url() -> str:
    return str(os.getenv("LANDCHECK_API_PUBLIC_URL") or "https://api.landcheck.online").strip().rstrip("/")


def _web_url() -> str:
    return str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").strip().rstrip("/")


def _decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _subscription_status_payload(subscription: EstateSubscription | None) -> dict:
    if subscription is None:
        return {"status": "none", "plan_key": None, "hazard_analysis": False}
    plan = ESTATE_PLANS.get(subscription.plan_key, {})
    return {
        "status": subscription.status,
        "plan_key": subscription.plan_key,
        "plan_label": plan.get("label"),
        "billing_cycle": subscription.billing_cycle,
        "amount": str(subscription.amount),
        "currency": subscription.currency,
        "payment_method": subscription.payment_method,
        "hazard_analysis": bool(plan.get("hazard_analysis")),
        "trial_ends_at": subscription.trial_ends_at,
        "current_period_end": subscription.current_period_end,
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "card_last4": subscription.card_last4,
        "card_brand": subscription.card_brand,
    }


def _account_for_access(db: Session, access: EstateAccess, organization_id: int) -> EstateAccount | None:
    """Resolve the account consuming the trial, never an account id from the client."""
    if access.principal.subject_type == "estate_account":
        try:
            account = db.get(EstateAccount, int(access.principal.subject_id))
        except (TypeError, ValueError):
            account = None
        if account and account.organization_id == organization_id and account.status == "active":
            return account
    return (
        db.query(EstateAccount)
        .filter(EstateAccount.organization_id == organization_id, EstateAccount.status == "active")
        .order_by(EstateAccount.created_at.asc(), EstateAccount.id.asc())
        .first()
    )


def _refund_verification(verify_data: dict, organization_id: int) -> None:
    transaction_id = str(verify_data.get("id") or "")
    if not transaction_id:
        return
    try:
        flw.refund_transaction(transaction_id, amount=VERIFICATION_CHARGE_AMOUNT)
    except Exception:
        logger.exception("Could not auto-refund the verification charge (org=%s)", organization_id)


@router.get("/status")
def billing_status(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, require_subscription=False)
    return _subscription_status_payload(get_subscription(db, organization_id))


@router.get("/plans")
def billing_plans():
    return {
        "trial_days": TRIAL_DAYS,
        "plans": {
            key: {"label": plan["label"], "monthly": str(plan["monthly"]), "yearly": str(plan["yearly"]), "hazard_analysis": plan["hazard_analysis"]}
            for key, plan in ESTATE_PLANS.items()
        },
    }


@router.get("/charges")
def billing_charges(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, require_subscription=False)
    rows = (
        db.query(EstateSubscriptionCharge)
        .filter(EstateSubscriptionCharge.organization_id == organization_id)
        .order_by(EstateSubscriptionCharge.attempted_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": row.id,
            "charge_type": row.charge_type,
            "amount": str(row.amount),
            "currency": row.currency,
            "status": row.status,
            "failure_reason": row.failure_reason,
            "attempted_at": row.attempted_at,
        }
        for row in rows
    ]


@router.post("/checkout")
def start_checkout(payload: ChoosePlanRequest, organization_id: int, request: Request, db: Session = Depends(get_db)):
    access = require_estate_access(db, request, organization_id, permission="billing.manage", require_subscription=False)
    plan_key = str(payload.plan_key or "").strip().lower()
    billing_cycle = str(payload.billing_cycle or "").strip().lower()
    if plan_key not in ESTATE_PLANS:
        raise HTTPException(422, "Choose Basic or Plus")
    if billing_cycle not in {"monthly", "yearly"}:
        raise HTTPException(422, "Choose monthly or yearly billing")
    organization = db.get(EstateOrganization, organization_id)
    if not organization:
        raise HTTPException(404, "Estate organization was not found")
    account = _account_for_access(db, access, organization_id)
    if not account:
        raise HTTPException(403, "An active Estate account is required to start a trial")

    existing = get_subscription(db, organization_id)
    if account.trial_claimed_at is not None or (existing and (existing.trial_ends_at is not None or existing.status in {"trialing", "active"})):
        raise HTTPException(409, "This account has already used its free trial. Use billing recovery to pay for the subscription.")

    email = str(account.email or organization.contact_email or "").strip()
    if not email:
        raise HTTPException(422, "This account has no billing email on file")

    tx_ref = new_tx_ref("VERIFY")
    link = flw.initiate_checkout(
        tx_ref=tx_ref,
        amount=VERIFICATION_CHARGE_AMOUNT,
        currency="NGN",
        email=email,
        name=organization.name,
        redirect_url=f"{_api_public_url()}/estates/billing/checkout/return",
        title="LandCheck Estates",
        description="Payment verification for your free trial - refunded immediately, not a real charge.",
        payment_options="card,banktransfer",
        bank_transfer_expiry=3600,
        meta={
            "purpose": "estate_subscription_verification",
            "organization_id": str(organization_id),
            "account_id": str(account.id),
            "customer_email": email,
            "plan_key": plan_key,
            "billing_cycle": billing_cycle,
        },
    )
    return {"checkout_url": link, "tx_ref": tx_ref}


def _complete_verification(db: Session, verify_data: dict) -> dict:
    meta = dict(verify_data.get("meta") or {})
    if str(meta.get("purpose") or "") != "estate_subscription_verification":
        return {"ok": True, "ignored": True}
    try:
        organization_id = int(meta.get("organization_id") or 0)
        account_id = int(meta.get("account_id") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "message": "Could not match this payment to an Estate account."}
    plan_key = str(meta.get("plan_key") or "")
    billing_cycle = str(meta.get("billing_cycle") or "")
    organization = db.get(EstateOrganization, organization_id)
    if account_id:
        account = db.query(EstateAccount).filter(EstateAccount.id == account_id, EstateAccount.organization_id == organization_id).with_for_update().one_or_none()
    else:
        account = (
            db.query(EstateAccount)
            .filter(EstateAccount.organization_id == organization_id, EstateAccount.status == "active")
            .order_by(EstateAccount.created_at.asc(), EstateAccount.id.asc())
            .with_for_update()
            .first()
        )
    if not organization or not account or plan_key not in ESTATE_PLANS or billing_cycle not in {"monthly", "yearly"}:
        return {"ok": False, "message": "Could not match this payment to an Estate account."}

    status = str(verify_data.get("status") or "").lower()
    charged_amount = _decimal(verify_data.get("charged_amount") or verify_data.get("amount"))
    currency = str(verify_data.get("currency") or "NGN").upper()
    if status not in {"successful", "succeeded"} or currency != "NGN" or charged_amount + Decimal("0.01") < VERIFICATION_CHARGE_AMOUNT:
        return {"ok": False, "message": "Card verification was not successful."}

    existing = db.query(EstateSubscription).filter(EstateSubscription.organization_id == organization_id).with_for_update().one_or_none()
    if account.trial_claimed_at is not None or (existing and (existing.trial_ends_at is not None or existing.status in {"trialing", "active"})):
        _refund_verification(verify_data, organization_id)
        db.commit()
        return {"ok": True, "already_used": True}

    card_token, card_last4, card_brand = flw.extract_card_token(verify_data)
    provider_payment_type = str(
        verify_data.get("payment_type")
        or verify_data.get("payment_method")
        or verify_data.get("payment_options")
        or ""
    ).strip().lower()
    payment_method = "bank_transfer" if "bank" in provider_payment_type or "transfer" in provider_payment_type else "card"
    if not card_token and payment_method == "card":
        _refund_verification(verify_data, organization_id)
        return {"ok": False, "message": "This card cannot be saved for recurring billing. Please use another card."}

    customer = dict(verify_data.get("customer") or {})
    email = str(meta.get("customer_email") or customer.get("email") or account.email or organization.contact_email or "").strip()
    try:
        subscription = start_trial(
            db,
            organization=organization,
            plan_key=plan_key,
            billing_cycle=billing_cycle,
            email=email,
            card_token=card_token,
            card_last4=card_last4,
            card_brand=card_brand,
            payment_method=payment_method,
        )
    except ValueError as exc:
        _refund_verification(verify_data, organization_id)
        return {"ok": False, "message": str(exc)}

    account.trial_claimed_at = datetime.now(timezone.utc)
    _refund_verification(verify_data, organization_id)
    db.commit()
    return {"ok": True, "status": subscription.status, "trial_ends_at": subscription.trial_ends_at}


@router.post("/payment-checkout")
def start_payment_checkout(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="billing.manage", require_subscription=False)
    organization = db.get(EstateOrganization, organization_id)
    subscription = get_subscription(db, organization_id)
    if not organization or not subscription or subscription.status not in {"past_due", "canceled", "expired"}:
        raise HTTPException(409, "There is no payment due for this organization")

    existing_pending = (
        db.query(EstateSubscriptionCharge)
        .filter(
            EstateSubscriptionCharge.subscription_id == subscription.id,
            EstateSubscriptionCharge.charge_type.in_(("retry", "trial_conversion", "renewal")),
            EstateSubscriptionCharge.status == "pending",
        )
        .order_by(EstateSubscriptionCharge.id.desc())
        .first()
    )
    if existing_pending and isinstance(existing_pending.flutterwave_payload, dict) and existing_pending.flutterwave_payload.get("checkout_url"):
        return {"checkout_url": existing_pending.flutterwave_payload["checkout_url"], "tx_ref": existing_pending.tx_ref}
    if existing_pending:
        existing_pending.status = "failed"
        existing_pending.failure_reason = "Replaced by a new customer-initiated payment checkout."

    email = str(subscription.flutterwave_customer_email or organization.contact_email or "").strip()
    if not email:
        raise HTTPException(422, "This organization has no billing email on file")
    tx_ref = new_tx_ref("PAY")
    charge = EstateSubscriptionCharge(
        subscription_id=subscription.id,
        organization_id=organization_id,
        charge_type="retry",
        amount=subscription.amount,
        currency=subscription.currency,
        status="pending",
        tx_ref=tx_ref,
    )
    db.add(charge)
    db.flush()
    try:
        link = flw.initiate_checkout(
            tx_ref=tx_ref,
            amount=charge.amount,
            currency=charge.currency,
            email=email,
            name=organization.name,
            redirect_url=f"{_api_public_url()}/estates/billing/checkout/return",
            title="LandCheck Estates",
            description=f"Payment for your {subscription.plan_key.title()} plan.",
            payment_options="banktransfer" if subscription.payment_method == "bank_transfer" else "card,banktransfer",
            bank_transfer_expiry=86400 if subscription.payment_method == "bank_transfer" else None,
            meta={
                "purpose": "estate_subscription_payment",
                "organization_id": str(organization_id),
                "subscription_id": str(subscription.id),
                "charge_id": str(charge.id),
                "customer_email": email,
            },
        )
    except Exception:
        db.rollback()
        raise
    charge.flutterwave_payload = {"checkout_url": link}
    db.commit()
    return {"checkout_url": link, "tx_ref": tx_ref}


def _find_charge(db: Session, verify_data: dict, tx_ref: str | None = None) -> EstateSubscriptionCharge | None:
    reference = str(tx_ref or verify_data.get("tx_ref") or "").strip()
    transaction_id = str(verify_data.get("id") or "").strip()
    filters = []
    if reference:
        filters.append(EstateSubscriptionCharge.tx_ref == reference)
    if transaction_id:
        filters.append(EstateSubscriptionCharge.flutterwave_transaction_id == transaction_id)
    if not filters:
        return None
    return db.query(EstateSubscriptionCharge).filter(or_(*filters)).with_for_update().first()


def _complete_subscription_payment(db: Session, verify_data: dict, tx_ref: str | None = None) -> dict:
    charge = _find_charge(db, verify_data, tx_ref)
    if not charge or charge.charge_type not in {"retry", "trial_conversion", "renewal", "plan_change"}:
        return {"ok": False, "message": "Could not match this payment to a subscription."}
    if charge.status == "success":
        return {"ok": True, "already_completed": True}

    subscription = db.query(EstateSubscription).filter(EstateSubscription.id == charge.subscription_id).with_for_update().one_or_none()
    if not subscription:
        charge.status = "failed"
        charge.failure_reason = "Subscription no longer exists."
        db.commit()
        return {"ok": False, "message": "Subscription no longer exists."}

    status = str(verify_data.get("status") or "").lower()
    amount = _decimal(verify_data.get("charged_amount") or verify_data.get("amount"))
    currency = str(verify_data.get("currency") or "").upper()
    charge_metadata = dict(charge.flutterwave_payload or {}) if isinstance(charge.flutterwave_payload, dict) else {}
    charge.flutterwave_transaction_id = str(verify_data.get("id") or "") or charge.flutterwave_transaction_id
    charge.flutterwave_payload = {**charge_metadata, "provider": verify_data or charge_metadata.get("provider")}
    if status in {"pending", "processing", "awaiting_authorization", "queued"}:
        charge.failure_reason = "Payment authorization is pending."
        db.commit()
        return {"ok": False, "pending": True}
    if status not in {"successful", "succeeded"} or currency != str(charge.currency).upper() or amount + Decimal("0.01") < Decimal(str(charge.amount)):
        charge.status = "failed"
        charge.failure_reason = str(verify_data.get("processor_response") or status or "Payment was not successful")
        db.commit()
        return {"ok": False, "message": "Payment was not successful."}

    card_token, card_last4, card_brand = flw.extract_card_token(verify_data)
    provider_payment_type = str(
        verify_data.get("payment_type")
        or verify_data.get("payment_method")
        or verify_data.get("payment_options")
        or ""
    ).strip().lower()
    if "bank" in provider_payment_type or "transfer" in provider_payment_type:
        subscription.payment_method = "bank_transfer"
    if card_token:
        subscription.card_token = card_token
        subscription.card_last4 = card_last4
        subscription.card_brand = card_brand
    customer = dict(verify_data.get("customer") or {})
    subscription.flutterwave_customer_email = str(customer.get("email") or subscription.flutterwave_customer_email or "").strip() or subscription.flutterwave_customer_email
    charge.status = "success"
    charge.failure_reason = None
    organization = db.get(EstateOrganization, subscription.organization_id)
    if charge.charge_type == "plan_change":
        new_plan_key = str(charge_metadata.get("plan_key") or "").strip().lower()
        if new_plan_key not in ESTATE_PLANS:
            charge.status = "failed"
            charge.failure_reason = "The requested plan change could not be identified."
            db.commit()
            return {"ok": False, "message": "The requested plan change could not be identified."}
        finalize_plan_change(
            db,
            subscription,
            new_plan_key=new_plan_key,
            organization=organization,
            charged_amount=Decimal(str(charge.amount)),
        )
    else:
        _handle_successful_charge(db, subscription, organization=organization)
    db.commit()
    return {"ok": True, "status": subscription.status}


@router.get("/checkout/return")
def checkout_return(transaction_id: str | None = None, tx_ref: str | None = None, status: str | None = None, db: Session = Depends(get_db)):
    """Re-verify the provider transaction, then redirect to the correct billing page."""
    is_payment = str(tx_ref or "").startswith("PAY-")
    outcome = "failed"
    if str(status or "").lower() == "cancelled":
        if is_payment and tx_ref:
            charge = db.query(EstateSubscriptionCharge).filter(EstateSubscriptionCharge.tx_ref == tx_ref, EstateSubscriptionCharge.status == "pending").with_for_update().first()
            if charge:
                charge.status = "failed"
                charge.failure_reason = "Checkout cancelled by customer."
                db.commit()
        target = "estates/billing" if is_payment else "estates/choose-plan"
        query_key = "payment_result" if is_payment else "result"
        return RedirectResponse(f"{_web_url()}/{target}?{query_key}=failed", status_code=302)

    if transaction_id or tx_ref:
        try:
            verify_data = flw.verify_transaction(transaction_id) if transaction_id else flw.verify_transaction_by_reference(tx_ref)  # type: ignore[arg-type]
            purpose = str((verify_data.get("meta") or {}).get("purpose") or "")
            is_payment = is_payment or purpose == "estate_subscription_payment"
            result = _complete_subscription_payment(db, verify_data, tx_ref) if is_payment else _complete_verification(db, verify_data)
            outcome = "success" if result.get("ok") else ("pending" if result.get("pending") else "failed")
        except Exception:
            logger.exception("Estate billing checkout-return verification failed (tx_ref=%s)", tx_ref)
    target = "estates/billing" if is_payment else "estates/choose-plan"
    query_key = "payment_result" if is_payment else "result"
    return RedirectResponse(f"{_web_url()}/{target}?{query_key}={outcome}", status_code=302)


@router.post("/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("verif-hash") or request.headers.get("flutterwave-signature")
    secret_hash_configured = bool(os.getenv("FLW_SECRET_HASH") or os.getenv("FLUTTERWAVE_SECRET_HASH"))
    if require_signed_flutterwave_webhooks() and not secret_hash_configured:
        raise HTTPException(503, "Flutterwave webhook signing is not configured")
    if secret_hash_configured and not flw.verify_webhook_signature(raw_body, signature):
        raise HTTPException(401, "Invalid webhook signature")
    payload = await request.json()
    if str(payload.get("event.type") or payload.get("event") or "") not in {"charge.completed", "CARD_TRANSACTION"} and str((payload.get("data") or {}).get("status") or "") == "":
        return {"ok": True, "ignored": True}
    data = dict(payload.get("data") or {})
    transaction_id = str(data.get("id") or "")
    tx_ref = str(data.get("tx_ref") or "")
    if not transaction_id and not tx_ref:
        return {"ok": True, "ignored": True}
    try:
        verify_data = flw.verify_transaction(transaction_id) if transaction_id else flw.verify_transaction_by_reference(tx_ref)
        purpose = str((verify_data.get("meta") or {}).get("purpose") or "")
        is_payment = purpose == "estate_subscription_payment" or bool(_find_charge(db, verify_data, tx_ref))
        result = _complete_subscription_payment(db, verify_data, tx_ref) if is_payment else _complete_verification(db, verify_data)
    except Exception:
        logger.exception("Estate billing webhook verification failed (tx_ref=%s)", tx_ref)
        return {"ok": False}
    return result


@router.post("/cancel")
def cancel(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="billing.manage", require_subscription=False)
    subscription = get_subscription(db, organization_id)
    if not subscription:
        raise HTTPException(404, "No subscription found for this organization")
    cancel_subscription(db, subscription)
    db.commit()
    return _subscription_status_payload(subscription)


@router.post("/change-plan")
def change(payload: ChangePlanRequest, organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, permission="billing.manage", require_subscription=False)
    plan_key = str(payload.plan_key or "").strip().lower()
    if plan_key not in ESTATE_PLANS:
        raise HTTPException(422, "Choose Basic or Plus")
    subscription = get_subscription(db, organization_id)
    if not subscription or subscription.status not in {"trialing", "active"}:
        raise HTTPException(409, "There is no active subscription to change")
    organization = db.get(EstateOrganization, organization_id)
    try:
        result = change_plan(db, subscription, new_plan_key=plan_key, organization=organization)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result.get("payment_status") == "failed":
        db.commit()
        raise HTTPException(402, "The plan-change payment was not successful. Your current plan is unchanged.")
    db.commit()
    response = _subscription_status_payload(subscription)
    response["plan_change_status"] = result.get("payment_status")
    response["plan_change_amount"] = str(result.get("charged_amount") or Decimal("0"))
    return response
