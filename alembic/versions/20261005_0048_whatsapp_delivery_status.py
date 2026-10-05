"""Track delivered/read status on outbound WhatsApp messages, for a WhatsApp-style tick indicator."""

from alembic import op


revision = "20261005_0048"
down_revision = "20261005_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_estate_wa_message_status", "estate_whatsapp_messages", type_="check")
    op.create_check_constraint(
        "ck_estate_wa_message_status",
        "estate_whatsapp_messages",
        "status IN ('received', 'sent', 'delivered', 'read', 'failed')",
    )


def downgrade() -> None:
    op.execute("UPDATE estate_whatsapp_messages SET status = 'sent' WHERE status IN ('delivered', 'read')")
    op.drop_constraint("ck_estate_wa_message_status", "estate_whatsapp_messages", type_="check")
    op.create_check_constraint(
        "ck_estate_wa_message_status",
        "estate_whatsapp_messages",
        "status IN ('received', 'sent', 'failed')",
    )
