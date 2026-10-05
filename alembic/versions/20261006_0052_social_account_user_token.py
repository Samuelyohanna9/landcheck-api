"""Keep the connecting person's user token - Instagram deletion needs it, not the Page token."""

from alembic import op
import sqlalchemy as sa


revision = "20261006_0052"
down_revision = "20261005_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_social_accounts", sa.Column("user_token_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_social_accounts", "user_token_enc")
