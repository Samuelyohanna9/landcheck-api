"""Track actual payouts of earned sales-agent commissions, separate from the amount earned."""

from alembic import op
import sqlalchemy as sa


revision = "20260915_0017"
down_revision = "20260915_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_commission_payouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("allocation_id", sa.Integer(), nullable=False),
        sa.Column("sales_agent_subject_type", sa.String(length=64), nullable=False),
        sa.Column("sales_agent_subject_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("payment_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_method", sa.String(length=80), nullable=False),
        sa.Column("reference_no", sa.String(length=160), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("paid_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("paid_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["allocation_id"], ["estate_allocations.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_estate_commission_payouts_allocation", "estate_commission_payouts", ["allocation_id"])
    op.create_index("idx_estate_commission_payouts_agent", "estate_commission_payouts", ["organization_id", "sales_agent_subject_type", "sales_agent_subject_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_commission_payouts_agent", table_name="estate_commission_payouts")
    op.drop_index("idx_estate_commission_payouts_allocation", table_name="estate_commission_payouts")
    op.drop_table("estate_commission_payouts")
