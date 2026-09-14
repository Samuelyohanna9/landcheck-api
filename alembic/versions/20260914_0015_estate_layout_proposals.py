"""Persist generated Estate concept layout proposals."""

from alembic import op
import sqlalchemy as sa


revision = "20260914_0015"
down_revision = "20260914_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_layout_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proposal_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("estate_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="review_required"),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("plot_candidates", sa.JSON(), nullable=False),
        sa.Column("feature_candidates", sa.JSON(), nullable=False),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("reviewed_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('review_required', 'approved', 'rejected')", name="ck_estate_layout_proposals_status"),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["estate_id"], ["estate_estates.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("proposal_uid", name="uq_estate_layout_proposals_uid"),
    )
    op.create_index("idx_estate_layout_proposals_estate", "estate_layout_proposals", ["estate_id", "created_at"])
    op.create_index("idx_estate_layout_proposals_organization", "estate_layout_proposals", ["organization_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_layout_proposals_organization", table_name="estate_layout_proposals")
    op.drop_index("idx_estate_layout_proposals_estate", table_name="estate_layout_proposals")
    op.drop_table("estate_layout_proposals")
