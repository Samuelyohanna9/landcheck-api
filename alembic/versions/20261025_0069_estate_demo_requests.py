"""Store private Estates demo enquiries submitted from the public site."""

from alembic import op
import sqlalchemy as sa


revision = "20261025_0069"
down_revision = "20261025_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_demo_requests",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("contact_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("source_path", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("notification_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_estate_demo_requests_idempotency_key"),
        sa.CheckConstraint("status IN ('new', 'contacted', 'closed')", name="ck_estate_demo_requests_status"),
        sa.CheckConstraint(
            "notification_status IN ('pending', 'sent', 'failed')",
            name="ck_estate_demo_requests_notification_status",
        ),
    )
    op.create_index(
        "ix_estate_demo_requests_created_at",
        "estate_demo_requests",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_estate_demo_requests_created_at", table_name="estate_demo_requests")
    op.drop_table("estate_demo_requests")
