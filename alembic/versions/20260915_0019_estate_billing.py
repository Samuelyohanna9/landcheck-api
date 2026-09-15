"""Add Estate subscription billing (Basic/Plus plans, trial + recurring charges) and password reset tokens."""

from alembic import op
import sqlalchemy as sa


revision = "20260915_0019"
down_revision = "20260915_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("plan_key", sa.String(length=16), nullable=False),
        sa.Column("billing_cycle", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="trialing"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="NGN"),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_charge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_charge_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("card_token", sa.String(length=255), nullable=True),
        sa.Column("card_last4", sa.String(length=8), nullable=True),
        sa.Column("card_brand", sa.String(length=32), nullable=True),
        sa.Column("flutterwave_customer_email", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", name="uq_estate_subscriptions_org"),
        sa.CheckConstraint("plan_key IN ('basic', 'plus')", name="ck_estate_subscriptions_plan"),
        sa.CheckConstraint("billing_cycle IN ('monthly', 'yearly')", name="ck_estate_subscriptions_cycle"),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')",
            name="ck_estate_subscriptions_status",
        ),
    )
    op.create_index("idx_estate_subscriptions_status", "estate_subscriptions", ["status"])

    op.create_table(
        "estate_subscription_charges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("charge_type", sa.String(length=24), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="NGN"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tx_ref", sa.String(length=64), nullable=False),
        sa.Column("flutterwave_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("flutterwave_payload", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["subscription_id"], ["estate_subscriptions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tx_ref", name="uq_estate_subscription_charges_tx_ref"),
        sa.CheckConstraint(
            "charge_type IN ('verification', 'trial_conversion', 'renewal', 'retry', 'plan_change')",
            name="ck_estate_subscription_charges_type",
        ),
        sa.CheckConstraint("status IN ('success', 'failed')", name="ck_estate_subscription_charges_status"),
    )
    op.create_index("idx_estate_subscription_charges_subscription", "estate_subscription_charges", ["subscription_id"])

    op.create_table(
        "estate_password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["account_id"], ["estate_accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_estate_password_reset_tokens_hash"),
    )
    op.create_index("idx_estate_password_reset_tokens_account", "estate_password_reset_tokens", ["account_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_password_reset_tokens_account", table_name="estate_password_reset_tokens")
    op.drop_table("estate_password_reset_tokens")
    op.drop_index("idx_estate_subscription_charges_subscription", table_name="estate_subscription_charges")
    op.drop_table("estate_subscription_charges")
    op.drop_index("idx_estate_subscriptions_status", table_name="estate_subscriptions")
    op.drop_table("estate_subscriptions")
