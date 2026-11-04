"""Add organization_id to every Estate table that doesn't already have one, backfilled from its
parent - the prerequisite for the next migration's RLS policies, so all 72 tables can use the same
flat organization_id = ANY(...) policy shape instead of per-table join/subquery policies."""

from alembic import op
import sqlalchemy as sa


revision = "20261104_0082"
down_revision = "20261103_0081"
branch_labels = None
depends_on = None


# (table, parent_table, fk_column_on_table) - organization_id is backfilled as
# SELECT organization_id FROM parent_table WHERE parent_table.id = table.fk_column_on_table.
_BACKFILL = (
    ("estate_approval_milestones", "estate_estates", "estate_id"),
    ("estate_blocks", "estate_estates", "estate_id"),
    ("estate_member_estate_scopes", "estate_estates", "estate_id"),
    ("estate_plots", "estate_estates", "estate_id"),
    ("estate_public_events", "estate_estates", "estate_id"),
    ("estate_boq_line_items", "estate_bills_of_quantities", "boq_id"),
    ("estate_construction_budget_lines", "estate_plot_construction_budgets", "budget_id"),
    ("estate_document_links", "estate_documents", "document_id"),
    ("estate_import_rows", "estate_import_batches", "batch_id"),
    ("estate_live_inspection_buyer_tokens", "estate_live_inspection_sessions", "session_id"),
)


def upgrade() -> None:
    for table, parent, fk_column in _BACKFILL:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('public.{table}') IS NULL THEN
                        RAISE EXCEPTION 'Required Estate table public.{table} is missing; run the one-shot legacy bootstrap before Alembic';
                    END IF;
                END $$;
                """
            )
        )
        op.add_column(table, sa.Column("organization_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text(
                f"""
                UPDATE public.{table} AS child
                SET organization_id = parent.organization_id
                FROM public.{parent} AS parent
                WHERE parent.id = child.{fk_column}
                """
            )
        )
        # A hard stop here (not a silent skip) is deliberate: a row this backfill couldn't match
        # would otherwise become invisible under every RLS policy the next migration installs -
        # better to fail the release than ship a silent data-visibility bug.
        op.execute(
            sa.text(
                f"""
                DO $$
                DECLARE
                    unmatched integer;
                BEGIN
                    SELECT count(*) INTO unmatched FROM public.{table} WHERE organization_id IS NULL;
                    IF unmatched > 0 THEN
                        RAISE EXCEPTION 'public.{table} has % row(s) with no resolvable organization_id via {parent}.{fk_column} - fix the data before this release can proceed', unmatched;
                    END IF;
                END $$;
                """
            )
        )
        op.alter_column(table, "organization_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_organization_id", table, "estate_organizations", ["organization_id"], ["id"], ondelete="CASCADE",
        )
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])


def downgrade() -> None:
    # Keep the backfilled columns during rollback - dropping a tenant column is not a safe
    # automatic downgrade and must be an explicit, separately reviewed change, same convention as
    # 20261101_0075_survey_rls_policies.py's downgrade.
    pass
