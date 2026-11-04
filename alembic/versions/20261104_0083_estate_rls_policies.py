"""Add fail-closed Row-Level Security policies across every Estates tenant table.

Mirrors 20261101_0075_survey_rls_policies.py's shape: a per-table DO block checks the table
exists, enables RLS (no FORCE - landcheck_migrator owns these tables and is exempt either way,
same reasoning as Survey's), then installs one tenant policy keyed on the session GUCs
app.estate_organization_ids / app.estate_bypass (set by app/routers/plots.py's get_db and
app/utils/row_security.py - see those files for why a route sets the narrow 'lookup' or
'superadmin' bypass instead of the real organization list). estate_organizations itself uses
id = ANY(...) instead of organization_id = ANY(...), since it IS the tenant. A handful of tables
also get a second, narrow policy for the one query a webhook/public-token/login route
legitimately runs before any organization is known - see the _LOOKUP_* tuples below."""

from alembic import op
import sqlalchemy as sa


revision = "20261104_0083"
down_revision = "20261104_0082"
branch_labels = None
depends_on = None

_TABLES = (
    "estate_accounts",
    "estate_agent_portal_tokens",
    "estate_allocation_charge_attempts",
    "estate_allocations",
    "estate_approval_milestones",
    "estate_audit_events",
    "estate_auth_sessions",
    "estate_bills_of_quantities",
    "estate_blocks",
    "estate_boq_line_items",
    "estate_canva_connections",
    "estate_canva_designs",
    "estate_canva_oauth_states",
    "estate_commission_payouts",
    "estate_commission_tiers",
    "estate_construction_budget_lines",
    "estate_construction_draw_requests",
    "estate_contractor_payments",
    "estate_contractors",
    "estate_customer_portal_tokens",
    "estate_customers",
    "estate_document_links",
    "estate_documents",
    "estate_dpa_acceptances",
    "estate_drawing_annotations",
    "estate_drawing_approvals",
    "estate_estates",
    "estate_field_inspections",
    "estate_geotech_survey_requests",
    "estate_hazard_assessments",
    "estate_import_batches",
    "estate_import_mapping_presets",
    "estate_import_reviews",
    "estate_import_rows",
    "estate_inspection_bookings",
    "estate_inspection_slots",
    "estate_layout_proposals",
    "estate_live_inspection_buyer_tokens",
    "estate_live_inspection_sessions",
    "estate_marketing_optins",
    "estate_member_estate_scopes",
    "estate_notification_logs",
    "estate_organization_entitlements",
    "estate_organization_members",
    "estate_organization_payout_accounts",
    "estate_organizations",
    "estate_payment_inbox",
    "estate_payment_issue_reports",
    "estate_payment_rules",
    "estate_payments",
    "estate_plot_construction_budgets",
    "estate_plots",
    "estate_progress_updates",
    "estate_public_events",
    "estate_public_reservation_requests",
    "estate_qr_campaigns",
    "estate_rfis",
    "estate_settlement_requests",
    "estate_site_inspections",
    "estate_snag_items",
    "estate_social_accounts",
    "estate_social_plans",
    "estate_social_posts",
    "estate_soil_assessments",
    "estate_spatial_features",
    "estate_staff_roles",
    "estate_staking_tasks",
    "estate_subscription_charges",
    "estate_subscriptions",
    "estate_survey_requests",
    "estate_whatsapp_messages",
    "estate_whatsapp_sends",
)

_LOOKUP_SELECT_TABLES = (
    "estate_subscription_charges",
    "estate_allocation_charge_attempts",
    "estate_canva_oauth_states",
    "estate_canva_designs",
    "estate_notification_logs",
    "estate_marketing_optins",
    "estate_customers",
    "estate_estates",
    "estate_agent_portal_tokens",
    "estate_customer_portal_tokens",
    "estate_inspection_bookings",
    "estate_live_inspection_buyer_tokens",
    "estate_accounts",
    "estate_auth_sessions",
    "estate_organization_members",
    "estate_social_posts",
    "estate_whatsapp_messages",
)

_LOOKUP_INSERT_TABLES = (
    "estate_organizations",
)

_LOOKUP_DELETE_TABLES = (
    "estate_social_accounts",
)


def upgrade() -> None:
    for table_name in _TABLES:
        # These identifiers are fixed constants in this migration, not user input.
        policy_name = f"lc_{table_name}_tenant_policy"
        tenant_column = "id" if table_name == "estate_organizations" else "organization_id"
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('public.{table_name}') IS NULL THEN
                        RAISE EXCEPTION 'Required Estate table public.{table_name} is missing; run the one-shot legacy bootstrap before Alembic';
                    END IF;
                    ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY;
                    DROP POLICY IF EXISTS {policy_name} ON public.{table_name};
                    CREATE POLICY {policy_name} ON public.{table_name}
                        FOR ALL
                        USING (
                            current_setting('app.estate_bypass', true) = 'superadmin'
                            OR {tenant_column} = ANY(
                                string_to_array(NULLIF(current_setting('app.estate_organization_ids', true), ''), ',')::int[]
                            )
                        )
                        WITH CHECK (
                            current_setting('app.estate_bypass', true) = 'superadmin'
                            OR {tenant_column} = ANY(
                                string_to_array(NULLIF(current_setting('app.estate_organization_ids', true), ''), ',')::int[]
                            )
                        );
                END $$;
                """
            )
        )

    for table_name in _LOOKUP_SELECT_TABLES:
        policy_name = f"lc_{table_name}_lookup_select_policy"
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    DROP POLICY IF EXISTS {policy_name} ON public.{table_name};
                    CREATE POLICY {policy_name} ON public.{table_name}
                        FOR SELECT
                        USING (current_setting('app.estate_bypass', true) = 'lookup');
                END $$;
                """
            )
        )

    for table_name in _LOOKUP_INSERT_TABLES:
        policy_name = f"lc_{table_name}_lookup_insert_policy"
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    DROP POLICY IF EXISTS {policy_name} ON public.{table_name};
                    CREATE POLICY {policy_name} ON public.{table_name}
                        FOR INSERT
                        WITH CHECK (current_setting('app.estate_bypass', true) = 'lookup');
                END $$;
                """
            )
        )

    for table_name in _LOOKUP_DELETE_TABLES:
        policy_name = f"lc_{table_name}_lookup_delete_policy"
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    DROP POLICY IF EXISTS {policy_name} ON public.{table_name};
                    CREATE POLICY {policy_name} ON public.{table_name}
                        FOR DELETE
                        USING (current_setting('app.estate_bypass', true) = 'lookup');
                END $$;
                """
            )
        )


def downgrade() -> None:
    # Keep the policies in place during application rollback - removing a tenant boundary is not
    # a safe automatic downgrade and must be an explicit, separately reviewed security change.
    # Same convention as 20261101_0075_survey_rls_policies.py.
    pass
