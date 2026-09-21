"""Keep campaign attribution on converted plot allocations."""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0029"
down_revision = "20260921_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_allocations", sa.Column("lead_source_code", sa.String(120), nullable=True))
    op.add_column("estate_allocations", sa.Column("lead_source_channel", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_allocations", "lead_source_channel")
    op.drop_column("estate_allocations", "lead_source_code")
