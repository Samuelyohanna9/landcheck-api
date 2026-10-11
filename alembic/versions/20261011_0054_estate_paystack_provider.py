"""Second payment gateway (Paystack) for Estate billing: add payment_provider to
estate_subscriptions/estate_subscription_charges, and rename the Flutterwave-specific
columns that already hold provider-agnostic data to generic names."""

from alembic import op
import sqlalchemy as sa


revision = "20261011_0054"
down_revision = "20261006_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "estate_subscriptions",
        sa.Column("payment_provider", sa.String(16), nullable=False, server_default="flutterwave"),
    )
    op.execute("UPDATE estate_subscriptions SET payment_provider = 'flutterwave'")
    op.create_check_constraint(
        "ck_estate_subscriptions_provider",
        "estate_subscriptions",
        "payment_provider IN ('flutterwave', 'paystack')",
    )
    op.alter_column("estate_subscriptions", "flutterwave_customer_email", new_column_name="billing_email")

    op.add_column(
        "estate_subscription_charges",
        sa.Column("payment_provider", sa.String(16), nullable=False, server_default="flutterwave"),
    )
    op.execute("UPDATE estate_subscription_charges SET payment_provider = 'flutterwave'")
    op.create_check_constraint(
        "ck_estate_subscription_charges_provider",
        "estate_subscription_charges",
        "payment_provider IN ('flutterwave', 'paystack')",
    )
    op.alter_column("estate_subscription_charges", "flutterwave_transaction_id", new_column_name="provider_transaction_id")
    op.alter_column("estate_subscription_charges", "flutterwave_payload", new_column_name="provider_payload")


def downgrade() -> None:
    op.alter_column("estate_subscription_charges", "provider_payload", new_column_name="flutterwave_payload")
    op.alter_column("estate_subscription_charges", "provider_transaction_id", new_column_name="flutterwave_transaction_id")
    op.drop_constraint("ck_estate_subscription_charges_provider", "estate_subscription_charges", type_="check")
    op.drop_column("estate_subscription_charges", "payment_provider")

    op.alter_column("estate_subscriptions", "billing_email", new_column_name="flutterwave_customer_email")
    op.drop_constraint("ck_estate_subscriptions_provider", "estate_subscriptions", type_="check")
    op.drop_column("estate_subscriptions", "payment_provider")
