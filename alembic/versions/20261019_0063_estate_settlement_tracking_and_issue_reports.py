"""Add settlement-tracking columns to estate_allocation_charge_attempts and a new
estate_payment_issue_reports table for the Accounting tab's "report a problem" action."""

from alembic import op
import sqlalchemy as sa


revision = "20261019_0063"
down_revision = "20261019_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_allocation_charge_attempts", sa.Column("settlement_status", sa.String(length=16), nullable=False, server_default="pending"))
    op.add_column("estate_allocation_charge_attempts", sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("settlement_checked_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "estate_payment_issue_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("charge_attempt_id", sa.Integer(), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=255), nullable=True),
        sa.Column("plot_number_snapshot", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("reported_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("reported_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("resolved_by_subject_id", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["charge_attempt_id"], ["estate_allocation_charge_attempts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["payment_id"], ["estate_payments.id"], ondelete="SET NULL"),
    )
    op.create_index("idx_estate_payment_issue_reports_org", "estate_payment_issue_reports", ["organization_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_payment_issue_reports_org", table_name="estate_payment_issue_reports")
    op.drop_table("estate_payment_issue_reports")
    op.drop_column("estate_allocation_charge_attempts", "settlement_checked_at")
    op.drop_column("estate_allocation_charge_attempts", "settled_at")
    op.drop_column("estate_allocation_charge_attempts", "settlement_status")
