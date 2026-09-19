from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.estate_auth import EstateAccount
from app.models.estate_billing import EstateSubscription, EstateSubscriptionCharge
from app.models.estate_foundation import EstateOrganization
from app.routers.estate_billing import _complete_subscription_payment, _complete_verification
from app.services.estates import estate_email
from app.services.estates.subscriptions import attempt_charge, start_trial
from app.utils import estate_flutterwave as flw


def _organization(db_session) -> EstateOrganization:
    organization = EstateOrganization(name="Billing Test", slug="billing-test", contact_email="billing@example.com")
    db_session.add(organization)
    db_session.commit()
    return organization


def test_tokenized_charge_includes_provider_required_country(monkeypatch):
    captured = {}

    def fake_call(method, path, *, payload=None, params=None):
        captured.update(payload or {})
        return {"data": {"status": "successful", "id": "tx-1"}}

    monkeypatch.setattr(flw, "call_flutterwave_api", fake_call)
    result = flw.charge_token(token="token-1", amount=Decimal("19500"), currency="NGN", email="billing@example.com", tx_ref="SUB-1")

    assert result["status"] == "successful"
    assert captured["country"] == "NG"
    assert captured["token"] == "token-1"


def test_trial_cannot_be_started_twice(monkeypatch, db_session):
    organization = _organization(db_session)
    monkeypatch.setattr(estate_email, "send_trial_started_email", lambda **kwargs: None)

    start_trial(
        db_session,
        organization=organization,
        plan_key="basic",
        billing_cycle="monthly",
        email="billing@example.com",
        card_token="token-1",
        card_last4="1234",
        card_brand="MASTERCARD",
    )
    db_session.commit()

    with pytest.raises(ValueError, match="already used"):
        start_trial(
            db_session,
            organization=organization,
            plan_key="plus",
            billing_cycle="monthly",
            email="billing@example.com",
            card_token="token-2",
            card_last4="5678",
            card_brand="VISA",
        )


def test_pending_provider_response_is_not_retried_as_a_failure(monkeypatch, db_session):
    organization = _organization(db_session)
    subscription = EstateSubscription(
        organization_id=organization.id,
        plan_key="basic",
        billing_cycle="monthly",
        status="trialing",
        amount=Decimal("19500"),
        currency="NGN",
        card_token="token-1",
        flutterwave_customer_email="billing@example.com",
    )
    db_session.add(subscription)
    db_session.commit()
    monkeypatch.setattr(estate_email, "send_payment_failed_email", lambda **kwargs: None)
    monkeypatch.setattr(flw, "charge_token", lambda **kwargs: {"status": "pending", "id": "tx-pending"})

    assert attempt_charge(db_session, subscription, charge_type="trial_conversion") is False
    db_session.commit()

    db_session.refresh(subscription)
    charge = db_session.query(EstateSubscriptionCharge).one()
    assert subscription.status == "past_due"
    assert subscription.next_retry_at is None
    assert charge.status == "pending"
    assert charge.flutterwave_transaction_id == "tx-pending"


def test_verification_without_token_is_refunded_and_does_not_start_trial(monkeypatch, db_session):
    organization = _organization(db_session)
    account = EstateAccount(
        organization_id=organization.id,
        email="billing@example.com",
        email_normalized="billing@example.com",
        full_name="Billing Owner",
        password_hash="hashed-password",
    )
    db_session.add(account)
    db_session.commit()
    refunded = []
    monkeypatch.setattr(flw, "refund_transaction", lambda transaction_id, amount=None: refunded.append((transaction_id, amount)))

    result = _complete_verification(
        db_session,
        {
            "id": "verify-1",
            "status": "successful",
            "amount": "50",
            "currency": "NGN",
            "meta": {
                "purpose": "estate_subscription_verification",
                "organization_id": str(organization.id),
                "account_id": str(account.id),
                "plan_key": "basic",
                "billing_cycle": "monthly",
            },
            "card": {},
        },
    )

    assert result["ok"] is False
    assert "another card" in result["message"]
    assert refunded and refunded[0][0] == "verify-1"
    assert db_session.query(EstateSubscription).count() == 0


def test_manual_recovery_payment_reactivates_subscription(monkeypatch, db_session):
    organization = _organization(db_session)
    subscription = EstateSubscription(
        organization_id=organization.id,
        plan_key="basic",
        billing_cycle="monthly",
        status="past_due",
        amount=Decimal("19500"),
        currency="NGN",
        flutterwave_customer_email="billing@example.com",
    )
    db_session.add(subscription)
    db_session.flush()
    charge = EstateSubscriptionCharge(
        subscription_id=subscription.id,
        organization_id=organization.id,
        charge_type="retry",
        amount=Decimal("19500"),
        currency="NGN",
        status="pending",
        tx_ref="PAY-RECOVERY-1",
    )
    db_session.add(charge)
    db_session.commit()
    monkeypatch.setattr(estate_email, "send_payment_receipt_email", lambda **kwargs: None)

    result = _complete_subscription_payment(
        db_session,
        {
            "id": "payment-1",
            "tx_ref": "PAY-RECOVERY-1",
            "status": "successful",
            "amount": "19500",
            "currency": "NGN",
            "customer": {"email": "billing@example.com"},
            "card": {"token": "token-new", "last_4digits": "9645", "type": "MASTERCARD"},
        },
    )

    db_session.refresh(subscription)
    db_session.refresh(charge)
    assert result["ok"] is True
    assert subscription.status == "active"
    assert subscription.card_token == "token-new"
    assert charge.status == "success"
