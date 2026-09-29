"""Soil Analysis storage - a standalone Estate-dashboard tool, deliberately separate from
estate_hazard_assessments (Soil Analysis is not a Hazard Analysis category)."""

from alembic import op
import sqlalchemy as sa


revision = "20261003_0042"
down_revision = "20261002_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_soil_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
        sa.Column("risk_class", sa.String(64), nullable=True),
        sa.Column("risk_score", sa.Numeric(8, 3), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("assessed_by_subject_type", sa.String(64), nullable=False),
        sa.Column("assessed_by_subject_id", sa.String(128), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_estate_soil_assessments_estate", "estate_soil_assessments", ["estate_id", "plot_id", "assessed_at"])


def downgrade() -> None:
    op.drop_index("ix_estate_soil_assessments_estate", table_name="estate_soil_assessments")
    op.drop_table("estate_soil_assessments")
