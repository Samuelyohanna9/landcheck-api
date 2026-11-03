"""Add Estate Bill of Quantities (BOQ) per plot."""

from alembic import op
import sqlalchemy as sa


revision = "20261103_0077"
down_revision = "20261103_0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_bills_of_quantities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("plot_id", name="uq_estate_bills_of_quantities_plot"),
    )
    op.create_table(
        "estate_boq_line_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("boq_id", sa.Integer(), sa.ForeignKey("estate_bills_of_quantities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("quantity", sa.Numeric(16, 3), nullable=False, server_default="0"),
        sa.Column("rate", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_estate_boq_line_items_boq", "estate_boq_line_items", ["boq_id"])


def downgrade() -> None:
    op.drop_table("estate_boq_line_items")
    op.drop_table("estate_bills_of_quantities")
