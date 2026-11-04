"""Allow Estate identity lookup to join the organization row under RLS.

The organization is not known until the authenticated session is resolved. This policy is
SELECT-only and is usable only while the application sets the narrow ``lookup`` context. Normal
Estate handlers clear that context before running tenant-scoped queries.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261104_0085"
down_revision = "20261104_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF to_regclass('public.estate_organizations') IS NULL THEN
                    RAISE EXCEPTION 'Required Estate table public.estate_organizations is missing';
                END IF;
                DROP POLICY IF EXISTS lc_estate_organizations_lookup_select_policy
                    ON public.estate_organizations;
                CREATE POLICY lc_estate_organizations_lookup_select_policy
                    ON public.estate_organizations
                    FOR SELECT
                    USING (current_setting('app.estate_bypass', true) = 'lookup');
            END
            $$;
            """
        )
    )


def downgrade() -> None:
    # Keep the identity lookup carve-out during application rollback. Remove it only in a
    # separately reviewed migration after the bootstrap path has been replaced.
    pass
