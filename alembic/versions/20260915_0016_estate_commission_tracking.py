"""Track sales-agent commissions on Estate allocations with a per-organization tier ladder."""

from alembic import op
import sqlalchemy as sa


revision = "20260915_0016"
down_revision = "20260914_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_allocations ADD COLUMN sales_agent_subject_type VARCHAR(64)")
    op.execute("ALTER TABLE estate_allocations ADD COLUMN sales_agent_subject_id VARCHAR(128)")
    op.execute("ALTER TABLE estate_allocations ADD COLUMN commission_tier_label VARCHAR(120)")
    op.execute("ALTER TABLE estate_allocations ADD COLUMN commission_rate_percent NUMERIC(5, 2)")
    op.execute("ALTER TABLE estate_allocations ADD COLUMN commission_amount NUMERIC(16, 2)")
    op.create_table(
        "estate_commission_tiers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("min_cumulative_sales", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("rate_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_estate_commission_tiers_organization", "estate_commission_tiers", ["organization_id"])
    op.create_index("idx_estate_allocations_sales_agent", "estate_allocations", ["organization_id", "sales_agent_subject_type", "sales_agent_subject_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_allocations_sales_agent", table_name="estate_allocations")
    op.drop_index("idx_estate_commission_tiers_organization", table_name="estate_commission_tiers")
    op.drop_table("estate_commission_tiers")
    op.execute("ALTER TABLE estate_allocations DROP COLUMN commission_amount")
    op.execute("ALTER TABLE estate_allocations DROP COLUMN commission_rate_percent")
    op.execute("ALTER TABLE estate_allocations DROP COLUMN commission_tier_label")
    op.execute("ALTER TABLE estate_allocations DROP COLUMN sales_agent_subject_id")
    op.execute("ALTER TABLE estate_allocations DROP COLUMN sales_agent_subject_type")
