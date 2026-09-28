"""Custom hero/cover photo for an estate's public page (falls back to the default when unset)."""

from alembic import op
import sqlalchemy as sa


revision = "20261001_0040"
down_revision = "20260930_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_estates", sa.Column("public_cover_object_key", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_estates", "public_cover_object_key")
