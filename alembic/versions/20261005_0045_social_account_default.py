"""Let a company choose which connected Facebook Page / Instagram account auto-posting uses."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0045"
down_revision = "20261005_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_social_accounts", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Every organisation that has exactly one connected account per provider today keeps working
    # exactly as before (that account is already the only one _account_for could ever pick) - mark it
    # default explicitly so the new "prefer is_default" query stays a no-op for them.
    op.execute(
        """
        UPDATE estate_social_accounts SET is_default = TRUE
        WHERE id IN (
            SELECT DISTINCT ON (organization_id, provider) id
            FROM estate_social_accounts
            WHERE status = 'active'
            ORDER BY organization_id, provider, id ASC
        )
        """
    )


def downgrade() -> None:
    op.drop_column("estate_social_accounts", "is_default")
