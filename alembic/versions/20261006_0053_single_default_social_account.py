"""Only one default Facebook Page and one default Instagram account per company, enforced by the database."""

from alembic import op


revision = "20261006_0053"
down_revision = "20261006_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the lowest-id default per company and provider; clear the rest.
    op.execute(
        """
        UPDATE estate_social_accounts a SET is_default = FALSE
        WHERE a.is_default
          AND EXISTS (
            SELECT 1 FROM estate_social_accounts b
            WHERE b.organization_id = a.organization_id AND b.provider = a.provider
              AND b.is_default AND b.id < a.id
          )
        """
    )
    # Companies with accounts but no default get their first account as default.
    op.execute(
        """
        UPDATE estate_social_accounts SET is_default = TRUE
        WHERE id IN (
            SELECT DISTINCT ON (organization_id, provider) id
            FROM estate_social_accounts
            WHERE status <> 'revoked'
            ORDER BY organization_id, provider, is_default DESC, id ASC
        )
        AND NOT EXISTS (
            SELECT 1 FROM estate_social_accounts c
            WHERE c.organization_id = estate_social_accounts.organization_id
              AND c.provider = estate_social_accounts.provider AND c.is_default
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_estate_social_one_default
        ON estate_social_accounts (organization_id, provider)
        WHERE is_default
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_estate_social_one_default")
