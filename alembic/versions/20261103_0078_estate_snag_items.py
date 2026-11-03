"""Add Estate snag items (defect/handover tracking)."""

from alembic import op
import sqlalchemy as sa


revision = "20261103_0078"
down_revision = "20261103_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_snag_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("assigned_to_contractor_id", sa.Integer(), sa.ForeignKey("estate_contractors.id", ondelete="SET NULL"), nullable=True),
        sa.Column("raised_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("raised_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("fixed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("verified_by_subject_id", sa.String(length=128), nullable=True),
        sa.CheckConstraint("status IN ('open', 'fixed', 'verified')", name="ck_estate_snag_items_status"),
    )
    op.create_index("ix_estate_snag_items_plot", "estate_snag_items", ["plot_id"])
    op.create_index("ix_estate_snag_items_org_status", "estate_snag_items", ["organization_id", "status"])


def downgrade() -> None:
    op.drop_table("estate_snag_items")
