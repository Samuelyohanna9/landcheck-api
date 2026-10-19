"""Add estate_allocation_charge_attempts - the online-payment ledger for a buyer paying for
their plot via Paystack/Flutterwave through the Buyer Portal."""

from alembic import op
import sqlalchemy as sa


revision = "20261019_0060"
down_revision = "20261018_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_allocation_charge_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("allocation_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("payment_provider", sa.String(length=32), nullable=False),
        sa.Column("tx_ref", sa.String(length=64), nullable=False),
        sa.Column("provider_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("provider_payload", sa.JSON(), nullable=True),
        sa.Column("resulting_payment_id", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["allocation_id"], ["estate_allocations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["estate_customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resulting_payment_id"], ["estate_payments.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tx_ref", name="uq_estate_allocation_charge_attempts_tx_ref"),
        sa.CheckConstraint("status IN ('pending', 'success', 'failed')", name="ck_estate_allocation_charge_attempts_status"),
    )
    op.create_index("idx_estate_allocation_charge_attempts_allocation", "estate_allocation_charge_attempts", ["allocation_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_allocation_charge_attempts_allocation", table_name="estate_allocation_charge_attempts")
    op.drop_table("estate_allocation_charge_attempts")
