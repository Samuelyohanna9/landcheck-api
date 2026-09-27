"""WhatsApp broadcasts carry a flyer design as their template header image."""

from alembic import op
import sqlalchemy as sa


revision = "20260929_0038"
down_revision = "20260928_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_whatsapp_sends", sa.Column("image_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_whatsapp_sends", "image_url")
