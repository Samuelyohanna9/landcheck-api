"""Add an unguessable share token to Estate allocations for the public "view your plot" link."""

from alembic import op
import sqlalchemy as sa


revision = "20260915_0018"
down_revision = "20260915_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_allocations ADD COLUMN share_token VARCHAR(64)")
    op.create_index("idx_estate_allocations_share_token", "estate_allocations", ["share_token"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_estate_allocations_share_token", table_name="estate_allocations")
    op.execute("ALTER TABLE estate_allocations DROP COLUMN share_token")
