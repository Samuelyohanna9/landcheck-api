"""Install the Survey tenant policies outside the API request path.

The API and Survey worker roles must not own these tables or execute policy DDL.  The controlled
migrator/bootstrap process installs the policy once, while runtime code only verifies that it is
present.  ``FORCE ROW LEVEL SECURITY`` is intentionally not enabled here; ownership is transferred
to the dedicated migration role by the reviewed role-provisioning procedure.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261101_0075"
down_revision = "20261031_0074"
branch_labels = None
depends_on = None


_TABLES = ("plots", "hazard_analysis_jobs", "survey_georeference_sessions")


def upgrade() -> None:
    for table_name in _TABLES:
        # These identifiers are fixed constants in this migration, not user input.
        policy_name = f"lc_{table_name}_owner_policy"
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('public.{table_name}') IS NULL THEN
                        RAISE EXCEPTION 'Required Survey table public.{table_name} is missing; run the one-shot legacy bootstrap before Alembic';
                    END IF;
                    ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY;
                    DROP POLICY IF EXISTS {policy_name} ON public.{table_name};
                    CREATE POLICY {policy_name} ON public.{table_name}
                        FOR ALL
                        USING (owner_user_id IS NULL OR owner_user_id = NULLIF(current_setting('app.survey_user_id', true), '')::bigint)
                        WITH CHECK (owner_user_id IS NULL OR owner_user_id = NULLIF(current_setting('app.survey_user_id', true), '')::bigint);
                END $$;
                """
            )
        )


def downgrade() -> None:
    # Keep the policies in place during application rollback. Removing a tenant boundary is not a
    # safe automatic downgrade and must be an explicit, separately reviewed security change.
    pass

