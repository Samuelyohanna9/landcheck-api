"""Add drawing version chaining + approval workflow, RFIs, and site inspections for Estate
construction tracking."""

from alembic import op
import sqlalchemy as sa


revision = "20261103_0080"
down_revision = "20261103_0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_documents", sa.Column("supersedes_document_id", sa.Integer(), sa.ForeignKey("estate_documents.id", ondelete="SET NULL"), nullable=True))

    op.create_table(
        "estate_drawing_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("estate_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="submitted"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("submitted_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("submitted_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by_subject_id", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("document_id", name="uq_estate_drawing_approvals_document"),
        sa.CheckConstraint("status IN ('submitted', 'approved', 'revision_requested')", name="ck_estate_drawing_approvals_status"),
    )

    op.create_table(
        "estate_rfis",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("raised_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("raised_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("answered_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'answered', 'closed')", name="ck_estate_rfis_status"),
    )
    op.create_index("ix_estate_rfis_plot_status", "estate_rfis", ["plot_id", "status"])

    op.create_table(
        "estate_site_inspections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inspector_subject_type", sa.String(length=64), nullable=False),
        sa.Column("inspector_subject_id", sa.String(length=128), nullable=False),
        sa.Column("visit_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("phase_at_inspection", sa.String(length=32), nullable=True),
        sa.Column("signed_off", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_estate_site_inspections_plot", "estate_site_inspections", ["plot_id"])


def downgrade() -> None:
    op.drop_table("estate_site_inspections")
    op.drop_table("estate_rfis")
    op.drop_table("estate_drawing_approvals")
    op.drop_column("estate_documents", "supersedes_document_id")
