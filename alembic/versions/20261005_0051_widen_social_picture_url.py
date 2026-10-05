"""Facebook's CDN picture URLs (with their auth query-string params) regularly exceed 500 characters."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0051"
down_revision = "20261005_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("estate_social_accounts", "picture_url", type_=sa.Text(), existing_type=sa.String(length=500))


def downgrade() -> None:
    op.alter_column("estate_social_accounts", "picture_url", type_=sa.String(length=500), existing_type=sa.Text())
