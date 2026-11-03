"""Add Estate construction/build tracking: per-plot budgets, contractors, draw requests, payments."""

from alembic import op
import sqlalchemy as sa


revision = "20261103_0076"
down_revision = "20261101_0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_plot_construction_budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("total_budget", sa.Numeric(16, 2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("plot_id", name="uq_estate_plot_construction_budgets_plot"),
    )
    op.create_index("ix_estate_plot_construction_budgets_org", "estate_plot_construction_budgets", ["organization_id"])

    op.create_table(
        "estate_construction_budget_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("budget_id", sa.Integer(), sa.ForeignKey("estate_plot_construction_budgets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("budgeted_amount", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("actual_amount", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_estate_construction_budget_lines_budget", "estate_construction_budget_lines", ["budget_id"])

    op.create_table(
        "estate_contractors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contractor_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("trade", sa.String(length=120), nullable=True),
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("contractor_uid", name="uq_estate_contractors_uid"),
    )
    op.create_index("ix_estate_contractors_org", "estate_contractors", ["organization_id"])

    op.create_table(
        "estate_construction_draw_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("budget_line_id", sa.Integer(), sa.ForeignKey("estate_construction_budget_lines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contractor_id", sa.Integer(), sa.ForeignKey("estate_contractors.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_requested", sa.Numeric(16, 2), nullable=False),
        sa.Column("percent_complete_claimed", sa.Integer(), nullable=True),
        sa.Column("retention_amount", sa.Numeric(16, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="requested"),
        sa.Column("requested_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("requested_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_uid", name="uq_estate_construction_draw_requests_uid"),
        sa.CheckConstraint("status IN ('requested', 'in_review', 'declined', 'paid')", name="ck_estate_construction_draw_requests_status"),
    )
    op.create_index("ix_estate_construction_draw_requests_org_status", "estate_construction_draw_requests", ["organization_id", "status"])

    op.create_table(
        "estate_contractor_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payment_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("draw_request_id", sa.Integer(), sa.ForeignKey("estate_construction_draw_requests.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("contractor_id", sa.Integer(), sa.ForeignKey("estate_contractors.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("payment_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_method", sa.String(length=80), nullable=False),
        sa.Column("reference_no", sa.String(length=160), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="confirmed"),
        sa.Column("recorded_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("recorded_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("payment_uid", name="uq_estate_contractor_payments_uid"),
    )
    op.create_index("ix_estate_contractor_payments_draw", "estate_contractor_payments", ["draw_request_id"])


def downgrade() -> None:
    op.drop_table("estate_contractor_payments")
    op.drop_table("estate_construction_draw_requests")
    op.drop_table("estate_contractors")
    op.drop_table("estate_construction_budget_lines")
    op.drop_table("estate_plot_construction_budgets")
