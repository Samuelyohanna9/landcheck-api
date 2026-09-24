"""Store reviewed Estate development forecast results."""

from alembic import op
import sqlalchemy as sa


revision = "20260924_0033"
down_revision = "20260922_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_estates", sa.Column("public_development_forecast", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_estates", "public_development_forecast")
