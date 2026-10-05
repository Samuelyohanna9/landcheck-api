"""Let a marketing post be sent with or without an auto-generated image."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0046"
down_revision = "20261005_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_social_posts", sa.Column("include_media", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("estate_social_posts", "include_media")
