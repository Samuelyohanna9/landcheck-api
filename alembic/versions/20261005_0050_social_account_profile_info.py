"""Show a connected Page/Instagram account's real profile picture and follower count, not just its name."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0050"
down_revision = "20261005_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_social_accounts", sa.Column("picture_url", sa.String(length=500), nullable=True))
    op.add_column("estate_social_accounts", sa.Column("followers_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_social_accounts", "followers_count")
    op.drop_column("estate_social_accounts", "picture_url")
