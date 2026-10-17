"""Government approvals tracker (survey plan / layout / Governor's Consent / C of O)."""

from alembic import op
import sqlalchemy as sa


revision = "20261018_0058"
down_revision = "20261018_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_approval_milestones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True),
        sa.Column("milestone_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="not_started"),
        sa.Column("reference_number", sa.String(length=120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "milestone_type IN ('survey_plan_approval', 'layout_approval', 'governors_consent', 'certificate_of_occupancy')",
            name="ck_estate_approval_milestone_type",
        ),
        sa.CheckConstraint(
            "status IN ('not_started', 'submitted', 'under_review', 'approved', 'rejected')",
            name="ck_estate_approval_milestone_status",
        ),
    )
    op.create_index("ix_estate_approval_milestones_estate_plot", "estate_approval_milestones", ["estate_id", "plot_id"])


def downgrade() -> None:
    op.drop_index("ix_estate_approval_milestones_estate_plot", table_name="estate_approval_milestones")
    op.drop_table("estate_approval_milestones")
