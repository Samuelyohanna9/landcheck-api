"""Let staff post their own uploaded photo, not only the auto-generated flyer."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0049"
down_revision = "20261005_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_social_posts", sa.Column("custom_image_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_social_posts", "custom_image_url")
