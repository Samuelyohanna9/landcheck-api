from __future__ import annotations

import logging
import os
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.models.estate_billing import EstateSubscription, EstateSubscriptionCharge
from app.models.estate_foundation import EstateOrganization
from app.routers.plots import get_db
from app.schemas.estate_billing import ChangePlanRequest, ChoosePlanRequest
from app.services.estates.authorization import require_estate_access
from app.services.estates.billing_plans import ESTATE_PLANS, TRIAL_DAYS, VERIFICATION_CHARGE_AMOUNT
from app.services.estates.subscriptions import (
    cancel_subscription,
    change_plan,
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
        "hazard_analysis": bool(plan.get("hazard_analysis")),
        "trial_ends_at": subscription.trial_ends_at,
        "current_period_end": subscription.current_period_end,
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "card_last4": subscription.card_last4,
        "card_brand": subscription.card_brand,
    }


@router.get("/status")
def billing_status(organization_id: int, request: Request, db: Session = Depends(get_db)):
    require_estate_access(db, request, organization_id, require_subscription=False)
    return _subscription_status_payload(get_subscription(db, organization_id))


@router.get("/plans")
def billing_plans():
    """Static plan catalogue - the same numbers the landing page's pricing section reads, so
    pricing only ever needs to change in one place (billing_plans.py)."""
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
    email = str(organization.contact_email or "").strip()
    if not email:
        raise HTTPException(422, "This organization has no billing email on file")

    tx_ref = new_tx_ref("VERIFY")
    redirect_url = f"{_api_public_url()}/estates/billing/checkout/return"
    link = flw.initiate_checkout(
        tx_ref=tx_ref,
        amount=VERIFICATION_CHARGE_AMOUNT,
        currency="NGN",
        email=email,
        name=organization.name,
        redirect_url=redirect_url,
        title="LandCheck Estates",
        description="Card verification for your free trial - refunded immediately, not a real charge.",
        meta={
            "purpose": "estate_subscription_verification",
            "organization_id": str(organization_id),
            "plan_key": plan_key,
            "billing_cycle": billing_cycle,
        },
    )
    return {"checkout_url": link, "tx_ref": tx_ref}


def _complete_verification(db: Session, verify_data: dict) -> dict:
    """Shared by both the webhook and the redirect-return handler, mirroring how Green's
    integration independently triggers verification from both paths - whichever arrives first
    wins, the other is a harmless no-op re-check."""
    meta = dict(verify_data.get("meta") or {})
    if str(meta.get("purpose") or "") != "estate_subscription_verification":
        return {"ok": True, "ignored": True}
    organization_id = int(meta.get("organization_id") or 0)
    plan_key = str(meta.get("plan_key") or "")
    billing_cycle = str(meta.get("billing_cycle") or "")
    organization = db.get(EstateOrganization, organization_id)
    if not organization or plan_key not in ESTATE_PLANS or billing_cycle not in {"monthly", "yearly"}:
        return {"ok": False, "message": "Could not match this payment to an Estate organization."}

    status = str(verify_data.get("status") or "").lower()
    charged_amount = Decimal(str(verify_data.get("charged_amount") or verify_data.get("amount") or 0))
    if status not in {"successful", "succeeded"} or charged_amount + Decimal("0.01") < VERIFICATION_CHARGE_AMOUNT:
        return {"ok": False, "message": "Card verification was not successful."}

    existing = get_subscription(db, organization_id)
    if existing and existing.status in {"trialing", "active"}:
        return {"ok": True, "already_active": True}

    card_token, card_last4, card_brand = flw.extract_card_token(verify_data)
    email = str(verify_data.get("customer", {}).get("email") or organization.contact_email or "")
    subscription = start_trial(
        db,
        organization=organization,
        plan_key=plan_key,
        billing_cycle=billing_cycle,
        email=email,
        card_token=card_token,
        card_last4=card_last4,
        card_brand=card_brand,
    )
    transaction_id = str(verify_data.get("id") or "")
    if transaction_id:
        try:
            flw.refund_transaction(transaction_id, amount=VERIFICATION_CHARGE_AMOUNT)
        except Exception:
            # Non-fatal: the trial has already started either way. A ₦50 verification charge
            # that fails to auto-refund is a manual-refund follow-up, not a blocker to access.
            logger.exception("Could not auto-refund the ₦50 verification charge (org=%s)", organization_id)
    db.commit()
    return {"ok": True, "status": subscription.status, "trial_ends_at": subscription.trial_ends_at}


@router.get("/checkout/return")
def checkout_return(transaction_id: str | None = None, tx_ref: str | None = None, status: str | None = None, db: Session = Depends(get_db)):
    """Flutterwave sends the CUSTOMER'S BROWSER here after checkout - this must answer with a real
    HTTP redirect back into the web app, never a JSON body (nothing reads it as an API response;
    a person is looking at whatever this returns)."""
    web_url = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    # `status` is Flutterwave's own claim from the redirect query string - always re-verified
    # server-side below rather than trusted directly, same as the webhook path.
    outcome = "failed"
    if str(status or "").lower() != "cancelled" and (transaction_id or tx_ref):
        try:
            verify_data = flw.verify_transaction(transaction_id) if transaction_id else flw.verify_transaction_by_reference(tx_ref)  # type: ignore[arg-type]
            result = _complete_verification(db, verify_data)
            outcome = "success" if result.get("ok") else "failed"
        except Exception:
            logger.exception("Estate billing checkout-return verification failed (tx_ref=%s)", tx_ref)
    return RedirectResponse(f"{web_url}/estates/choose-plan?result={outcome}", status_code=302)


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
    try:
        verify_data = flw.verify_transaction(transaction_id) if transaction_id else flw.verify_transaction_by_reference(tx_ref)
        result = _complete_verification(db, verify_data)
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
    change_plan(db, subscription, new_plan_key=plan_key)
    db.commit()
    return _subscription_status_payload(subscription)
