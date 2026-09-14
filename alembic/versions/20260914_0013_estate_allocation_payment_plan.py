"""Store agreed price and payment plan details on Estate allocations."""

from alembic import op


revision = "20260914_0013"
down_revision = "20260914_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_allocations ADD COLUMN payment_plan TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE estate_allocations DROP COLUMN payment_plan")
