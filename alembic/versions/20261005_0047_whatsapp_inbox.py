"""A real inbox for inbound WhatsApp messages, instead of discarding everything but STOP replies."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0047"
down_revision = "20261005_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_whatsapp_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("phone_digits", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("message_type", sa.String(length=16), nullable=False, server_default="text"),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_id", sa.String(length=120), nullable=True),
        sa.Column("media_mime_type", sa.String(length=80), nullable=True),
        sa.Column("wa_message_id", sa.String(length=120), nullable=True, unique=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="received"),
        sa.Column("sent_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("sent_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("read_by_staff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_estate_wa_message_direction"),
        sa.CheckConstraint("status IN ('received', 'sent', 'failed')", name="ck_estate_wa_message_status"),
    )
    op.create_index("ix_estate_wa_messages_estate_phone", "estate_whatsapp_messages", ["estate_id", "phone_digits", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_estate_wa_messages_estate_phone", table_name="estate_whatsapp_messages")
    op.drop_table("estate_whatsapp_messages")
