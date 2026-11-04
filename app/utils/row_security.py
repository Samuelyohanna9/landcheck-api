from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine


def set_survey_user_context(db: Session, user_id: int | None) -> None:
    if engine.dialect.name != "postgresql":
        return
    normalized_user_id = int(user_id) if user_id is not None else None
    # Session.commit() returns its connection to the pool.  db.py reapplies this intent when the
    # Session begins another transaction, while the pool listeners scrub the physical connection
    # so the intent cannot cross into a different Session.
    db.info["survey_user_id"] = normalized_user_id
    db.execute(
        text("SELECT set_config('app.survey_user_id', :user_id, false)"),
        {"user_id": str(normalized_user_id) if normalized_user_id is not None else ""},
    )


def clear_survey_user_context(db: Session) -> None:
    """Explicitly clear Survey context before a request/job session is returned to the pool."""

    if engine.dialect.name != "postgresql":
        db.info.pop("survey_user_id", None)
        return
    try:
        db.rollback()
        db.execute(text("RESET app.survey_user_id"))
        db.info.pop("survey_user_id", None)
        db.commit()
    except Exception:
        db.info.pop("survey_user_id", None)
        db.rollback()
        raise


def verify_survey_row_security(db: Session) -> None:
    """Verify that the migration-installed Survey RLS boundary is present.

    Runtime API and worker roles are deliberately not allowed to create/drop policies.  The
    migration/bootstrap role installs them once, and this read-only check makes a missing policy
    fail closed during startup or staging validation.
    """

    if engine.dialect.name != "postgresql":
        return

    expected_tables = ("plots", "hazard_analysis_jobs", "survey_georeference_sessions")
    rows = db.execute(
        text(
            """
            SELECT c.relname AS table_name,
                   c.relrowsecurity AS rls_enabled,
                   c.relforcerowsecurity AS force_rls
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(:table_names)
            """
        ),
        {
            "table_names": list(expected_tables),
        },
    ).mappings().all()
    by_table = {str(row["table_name"]): row for row in rows}
    missing = [name for name in expected_tables if name not in by_table]
    if missing:
        raise RuntimeError(
            "Required Survey tables are missing: " + ", ".join(missing)
            + ". Run the one-shot migration/bootstrap process before starting the API."
        )

    policy_rows = db.execute(
        text(
            """
            SELECT tablename, policyname
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = ANY(:table_names)
              AND policyname = ANY(:policy_names)
            """
        ),
        {
            "table_names": list(expected_tables),
            "policy_names": [f"lc_{name}_owner_policy" for name in expected_tables],
        },
    ).mappings().all()
    installed = {(str(row["tablename"]), str(row["policyname"])) for row in policy_rows}
    missing_policies = [
        f"{table}:{table_policy}"
        for table in expected_tables
        for table_policy in (f"lc_{table}_owner_policy",)
        if (table, table_policy) not in installed
    ]
    not_enabled = [
        table
        for table in expected_tables
        if not bool(by_table[table]["rls_enabled"])
    ]
    if missing_policies or not_enabled:
        details = []
        if not_enabled:
            details.append("RLS disabled on " + ", ".join(not_enabled))
        if missing_policies:
            details.append("missing policies " + ", ".join(missing_policies))
        raise RuntimeError("Survey RLS verification failed: " + "; ".join(details))


def ensure_survey_row_security(db: Session) -> None:
    """Backward-compatible alias for the read-only verification function."""
    verify_survey_row_security(db)


# ── Estates tenant context (app.estate_organization_ids / app.estate_bypass) ────────────────────
#
# Every table listed here gets a fail-closed RLS policy. estate_demo_requests (a pre-organization
# public sales lead - no tenant exists yet) and estate_email_verification_tokens/
# estate_password_reset_tokens (scoped by login account, not by organization - one account can
# belong to several organizations) are deliberately excluded: organization-tenancy RLS doesn't
# apply to them.
ESTATE_RLS_TABLES: tuple[str, ...] = (
    "estate_accounts", "estate_agent_portal_tokens", "estate_allocation_charge_attempts",
    "estate_allocations", "estate_approval_milestones", "estate_audit_events", "estate_auth_sessions",
    "estate_bills_of_quantities", "estate_blocks", "estate_boq_line_items", "estate_canva_connections",
    "estate_canva_designs", "estate_canva_oauth_states", "estate_commission_payouts",
    "estate_commission_tiers", "estate_construction_budget_lines", "estate_construction_draw_requests",
    "estate_contractor_payments", "estate_contractors", "estate_customer_portal_tokens",
    "estate_customers", "estate_document_links", "estate_documents", "estate_dpa_acceptances",
    "estate_drawing_annotations", "estate_drawing_approvals", "estate_estates",
    "estate_field_inspections", "estate_geotech_survey_requests", "estate_hazard_assessments",
    "estate_import_batches", "estate_import_mapping_presets", "estate_import_reviews",
    "estate_import_rows", "estate_inspection_bookings", "estate_inspection_slots",
    "estate_layout_proposals", "estate_live_inspection_buyer_tokens", "estate_live_inspection_sessions",
    "estate_marketing_optins", "estate_member_estate_scopes", "estate_notification_logs",
    "estate_organization_entitlements", "estate_organization_members", "estate_organization_payout_accounts",
    "estate_organizations", "estate_payment_inbox", "estate_payment_issue_reports", "estate_payment_rules",
    "estate_payments", "estate_plot_construction_budgets", "estate_plots", "estate_progress_updates",
    "estate_public_events", "estate_public_reservation_requests", "estate_qr_campaigns", "estate_rfis",
    "estate_settlement_requests", "estate_site_inspections", "estate_snag_items", "estate_social_accounts",
    "estate_social_plans", "estate_social_posts", "estate_soil_assessments", "estate_spatial_features",
    "estate_staff_roles", "estate_staking_tasks", "estate_subscription_charges", "estate_subscriptions",
    "estate_survey_requests", "estate_whatsapp_messages", "estate_whatsapp_sends",
)

# Tables where a request must read (or, for estate_organizations, insert) a row before any
# organization_id is knowable - a webhook matching by tx_ref, a portal/booking/live-inspection
# token being resolved by its hash, the public storefront resolving an estate by its slug, or
# login/registration resolving an account by email. See app.estate_bypass = 'lookup' below.
ESTATE_LOOKUP_SELECT_TABLES: tuple[str, ...] = (
    "estate_subscription_charges", "estate_allocation_charge_attempts", "estate_canva_oauth_states",
    "estate_canva_designs", "estate_notification_logs", "estate_marketing_optins", "estate_customers",
    "estate_estates", "estate_agent_portal_tokens", "estate_customer_portal_tokens",
    "estate_inspection_bookings", "estate_live_inspection_buyer_tokens", "estate_accounts",
    "estate_auth_sessions", "estate_organization_members", "estate_social_posts", "estate_whatsapp_messages",
)
# estate_organizations also needs INSERT under the 'lookup' bypass: registration creates a brand
# new organization row, whose id can't already be in the (not-yet-known) allow-list.
ESTATE_LOOKUP_INSERT_TABLES: tuple[str, ...] = ("estate_organizations",)
# estate_social_accounts also needs DELETE under 'lookup': Meta's deauthorize/data-deletion
# webhooks remove a connected account found by facebook_user_id, with no organization context.
ESTATE_LOOKUP_DELETE_TABLES: tuple[str, ...] = ("estate_social_accounts",)


def _session_is_postgres(db: Session) -> bool:
    """Checks the dialect of the SESSION's own bind, not the global app.db.engine import - a test
    fixture routinely binds its Session to an isolated SQLite engine for speed, independent of
    whatever the global engine (read from DATABASE_URL) happens to be. Using the global engine's
    dialect here would run a Postgres-only `SELECT set_config(...)` against that SQLite session
    and fail outright."""
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        return engine.dialect.name == "postgresql"


def set_estate_tenant_context(db: Session, organization_ids: list[int] | None) -> None:
    """Sets app.estate_organization_ids to every organization the current identity belongs to
    (resolved once per request in get_db - see app/routers/plots.py), or clears it for an
    unauthenticated/non-Estates request. RLS policies on ESTATE_RLS_TABLES use this as a
    defense-in-depth backstop alongside (never instead of) the existing Python-level
    require_estate_access* checks, which still do the precise single-org + permission +
    subscription enforcement exactly as before."""
    if not _session_is_postgres(db):
        return
    ids = sorted({int(value) for value in organization_ids}) if organization_ids else []
    db.info["estate_organization_ids"] = ids
    db.execute(
        text("SELECT set_config('app.estate_organization_ids', :ids, false)"),
        {"ids": ",".join(str(value) for value in ids)},
    )


def set_estate_bypass(db: Session, mode: str | None) -> None:
    """Sets app.estate_bypass to '' / 'superadmin' / 'lookup'. 'superadmin' grants every
    ESTATE_RLS_TABLES policy's full OR-branch (require_super_admin_request, and each trusted
    scheduled job's own session for its whole duration). 'lookup' only grants the narrow,
    SELECT-only (or, for the two tables above, INSERT/DELETE) OR-branch used by webhook and
    public-token routes for their first, identity-less lookup - callers must switch to the real
    organization_id via set_estate_tenant_context the moment it's known, before any other query."""
    if not _session_is_postgres(db):
        return
    value = mode or ""
    db.info["estate_bypass"] = value
    db.execute(text("SELECT set_config('app.estate_bypass', :mode, false)"), {"mode": value})


def clear_estate_tenant_context(db: Session) -> None:
    """Explicitly clear Estates tenant context before a request/job session is returned to the
    pool - mirrors clear_survey_user_context."""
    if not _session_is_postgres(db):
        db.info.pop("estate_organization_ids", None)
        db.info.pop("estate_bypass", None)
        return
    try:
        db.rollback()
        db.execute(text("RESET app.estate_organization_ids"))
        db.execute(text("RESET app.estate_bypass"))
        db.info.pop("estate_organization_ids", None)
        db.info.pop("estate_bypass", None)
        db.commit()
    except Exception:
        db.info.pop("estate_organization_ids", None)
        db.info.pop("estate_bypass", None)
        db.rollback()
        raise


def verify_estate_row_security(db: Session) -> None:
    """Verify that the migration-installed Estates RLS boundary is present on every table in
    ESTATE_RLS_TABLES - same fail-closed startup check as verify_survey_row_security, generalized
    to a 72-table list instead of 3."""

    if not _session_is_postgres(db):
        return

    expected_tables = ESTATE_RLS_TABLES
    rows = db.execute(
        text(
            """
            SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(:table_names)
            """
        ),
        {"table_names": list(expected_tables)},
    ).mappings().all()
    by_table = {str(row["table_name"]): row for row in rows}
    missing = [name for name in expected_tables if name not in by_table]
    if missing:
        raise RuntimeError(
            "Required Estate tables are missing: " + ", ".join(missing)
            + ". Run the one-shot migration/bootstrap process before starting the API."
        )

    policy_rows = db.execute(
        text(
            """
            SELECT tablename, policyname
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = ANY(:table_names)
              AND policyname = ANY(:policy_names)
            """
        ),
        {
            "table_names": list(expected_tables),
            "policy_names": [f"lc_{name}_tenant_policy" for name in expected_tables],
        },
    ).mappings().all()
    installed = {(str(row["tablename"]), str(row["policyname"])) for row in policy_rows}
    missing_policies = [
        f"{table}:lc_{table}_tenant_policy" for table in expected_tables
        if (table, f"lc_{table}_tenant_policy") not in installed
    ]
    lookup_tables = ("estate_auth_sessions", "estate_organization_members")
    lookup_policy_rows = db.execute(
        text(
            """
            SELECT tablename, policyname
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = ANY(:table_names)
              AND policyname = ANY(:policy_names)
            """
        ),
        {
            "table_names": list(lookup_tables),
            "policy_names": [f"lc_{name}_lookup_select_policy" for name in lookup_tables],
        },
    ).mappings().all()
    installed_lookup = {(str(row["tablename"]), str(row["policyname"])) for row in lookup_policy_rows}
    missing_lookup_policies = [
        f"{table}:lc_{table}_lookup_select_policy" for table in lookup_tables
        if (table, f"lc_{table}_lookup_select_policy") not in installed_lookup
    ]
    not_enabled = [table for table in expected_tables if not bool(by_table[table]["rls_enabled"])]
    if missing_policies or missing_lookup_policies or not_enabled:
        details = []
        if not_enabled:
            details.append("RLS disabled on " + ", ".join(not_enabled))
        if missing_policies:
            details.append("missing policies " + ", ".join(missing_policies))
        if missing_lookup_policies:
            details.append("missing lookup policies " + ", ".join(missing_lookup_policies))
        raise RuntimeError("Estate RLS verification failed: " + "; ".join(details))


def ensure_estate_row_security(db: Session) -> None:
    """Backward-compatible alias for the read-only verification function."""

    verify_estate_row_security(db)
