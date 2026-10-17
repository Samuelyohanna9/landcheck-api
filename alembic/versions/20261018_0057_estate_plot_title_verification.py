"""Staff-asserted per-plot title verification badge."""

from alembic import op
import sqlalchemy as sa


revision = "20261018_0057"
down_revision = "20261018_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_plots", sa.Column("title_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("estate_plots", sa.Column("title_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_plots", sa.Column("title_verified_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("estate_plots", "title_verified_note")
    op.drop_column("estate_plots", "title_verified_at")
    op.drop_column("estate_plots", "title_verified")
