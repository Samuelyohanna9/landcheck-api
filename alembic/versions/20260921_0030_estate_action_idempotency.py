"""Prevent duplicate Estate financial and allocation actions on retries."""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0030"
down_revision = "20260921_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_allocations", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("estate_payments", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("estate_commission_payouts", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX uq_estate_allocation_idempotency "
        "ON estate_allocations (organization_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_estate_payment_idempotency "
        "ON estate_payments (organization_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_estate_commission_payout_idempotency "
        "ON estate_commission_payouts (organization_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("uq_estate_commission_payout_idempotency", table_name="estate_commission_payouts")
    op.drop_index("uq_estate_payment_idempotency", table_name="estate_payments")
    op.drop_index("uq_estate_allocation_idempotency", table_name="estate_allocations")
    op.drop_column("estate_commission_payouts", "idempotency_key")
    op.drop_column("estate_payments", "idempotency_key")
    op.drop_column("estate_allocations", "idempotency_key")
