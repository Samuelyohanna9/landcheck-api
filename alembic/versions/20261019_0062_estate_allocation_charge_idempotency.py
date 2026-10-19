"""Prevent duplicate buyer online plot payments on retries/double-clicks - same convention as
20260921_0030's idempotency keys for manual payments/allocations."""

from alembic import op
import sqlalchemy as sa


revision = "20261019_0062"
down_revision = "20261019_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_allocation_charge_attempts", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX uq_estate_allocation_charge_attempt_idempotency "
        "ON estate_allocation_charge_attempts (organization_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("uq_estate_allocation_charge_attempt_idempotency", table_name="estate_allocation_charge_attempts")
    op.drop_column("estate_allocation_charge_attempts", "idempotency_key")
