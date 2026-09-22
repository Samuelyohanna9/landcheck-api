from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func, text

from app.db_base import Base


class EstateSubscription(Base):
    """One row per organization - reused across cancel/resubscribe cycles, not recreated. The
    `amount` is locked in at subscribe/plan-change time so a future price change never
    retroactively re-bills an existing subscriber a different amount than they agreed to."""

    __tablename__ = "estate_subscriptions"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False, unique=True)
    plan_key = Column(String(16), nullable=False)
    billing_cycle = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="trialing")
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(8), nullable=False, default="NGN")
    payment_method = Column(String(24), nullable=False, default="card", server_default="card")

    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    next_charge_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    failed_charge_attempts = Column(Integer, nullable=False, default=0)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    trial_reminder_sent_for = Column(DateTime(timezone=True), nullable=True)
    renewal_reminder_sent_for = Column(DateTime(timezone=True), nullable=True)

    card_token = Column(String(255), nullable=True)
    card_last4 = Column(String(8), nullable=True)
    card_brand = Column(String(32), nullable=True)
    flutterwave_customer_email = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("plan_key IN ('basic', 'plus')", name="ck_estate_subscriptions_plan"),
        CheckConstraint("billing_cycle IN ('monthly', 'yearly')", name="ck_estate_subscriptions_cycle"),
        CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')",
            name="ck_estate_subscriptions_status",
        ),
        CheckConstraint(
            "payment_method IN ('card', 'bank_transfer')",
            name="ck_estate_subscriptions_payment_method",
        ),
    )


class EstateSubscriptionCharge(Base):
    """Append-only billing ledger - every charge attempt (trial conversion, renewal, dunning
    retry) gets its own row here regardless of outcome, independent of the subscription's own
    current-state columns which only reflect the latest attempt."""

    __tablename__ = "estate_subscription_charges"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("estate_subscriptions.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Integer, ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False)
    charge_type = Column(String(24), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(8), nullable=False, default="NGN")
    status = Column(String(16), nullable=False)
    tx_ref = Column(String(64), nullable=False, unique=True)
    flutterwave_transaction_id = Column(String(64), nullable=True)
    flutterwave_payload = Column(JSON, nullable=True)
    failure_reason = Column(Text, nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "charge_type IN ('verification', 'trial_conversion', 'renewal', 'retry', 'plan_change')",
            name="ck_estate_subscription_charges_type",
        ),
        CheckConstraint("status IN ('pending', 'success', 'failed')", name="ck_estate_subscription_charges_status"),
        # A subscription's trial can only convert once - a second attempt after a failure is a
        # dunning "retry", never a second "trial_conversion" row. Enforced in the DB, not just in
        # application code, so a scheduler race can never double-charge a trial conversion.
        Index(
            "ux_estate_subscription_charges_one_trial_conversion",
            "subscription_id",
            unique=True,
            postgresql_where=text("charge_type = 'trial_conversion'"),
        ),
    )


class EstatePasswordResetToken(Base):
    """Single-use, hashed reset tokens - same hash-of-random-token pattern as
    EstateAuthSession.access_token_hash in identity.py, so the raw token is never stored."""

    __tablename__ = "estate_password_reset_tokens"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("estate_accounts.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
