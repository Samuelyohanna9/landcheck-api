"""Allow the Estate request bootstrap to resolve identity under RLS.

The 0083 tenant policies correctly fail closed when no organization context is present, but the
first authenticated Estate request must read its session and membership before that context can be
known. These two policies are SELECT-only and require the application to set the short-lived
``app.estate_bypass = 'lookup'`` value; request handlers switch to the organization allow-list
before yielding their database session.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261104_0084"
down_revision = "20261104_0083"
branch_labels = None
depends_on = None


_TABLES = ("estate_auth_sessions", "estate_organization_members")


def upgrade() -> None:
    for table_name in _TABLES:
        policy_name = f"lc_{table_name}_lookup_select_policy"
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('public.{table_name}') IS NULL THEN
                        RAISE EXCEPTION 'Required Estate table public.{table_name} is missing';
                    END IF;
                    DROP POLICY IF EXISTS {policy_name} ON public.{table_name};
                    CREATE POLICY {policy_name} ON public.{table_name}
                        FOR SELECT
                        USING (current_setting('app.estate_bypass', true) = 'lookup');
                END $$;
                """
            )
        )


def downgrade() -> None:
    # Removing this carve-out would make authenticated Estate requests fail closed again. Keep it
    # during application rollback; remove only in a separately reviewed security migration.
    pass
