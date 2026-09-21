"""Add structured payment reminder state and customer notification delivery log."""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0028"
down_revision = "20260921_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "estate_allocations",
        sa.Column("payment_reminder_sent_for_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "estate_notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("estate_allocations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", sa.String(32), nullable=False, server_default="email"),
        sa.Column("event_key", sa.String(80), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=True),
        sa.Column("recipient_name", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('sent', 'failed', 'skipped')", name="ck_estate_notification_logs_status"),
    )
    op.create_index("ix_estate_notification_logs_estate_created", "estate_notification_logs", ["estate_id", "created_at"])
    op.create_index("ix_estate_notification_logs_org_created", "estate_notification_logs", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_estate_notification_logs_org_created", table_name="estate_notification_logs")
    op.drop_index("ix_estate_notification_logs_estate_created", table_name="estate_notification_logs")
    op.drop_table("estate_notification_logs")
    op.drop_column("estate_allocations", "payment_reminder_sent_for_due_at")
