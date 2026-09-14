"""Add Estate project metadata and the developer development lifecycle."""

from alembic import op


revision = "20260914_0012"
down_revision = "20260914_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_estates ADD COLUMN project_reference VARCHAR(120)")
    op.execute("ALTER TABLE estate_estates ADD COLUMN project_owner VARCHAR(255)")
    op.execute("ALTER TABLE estate_estates ADD COLUMN ownership_details TEXT")
    op.execute("UPDATE estate_plots SET development_status = 'under_construction' WHERE development_status = 'in_progress'")
    op.execute("ALTER TABLE estate_plots DROP CONSTRAINT IF EXISTS ck_estate_plot_development_status")
    op.execute(
        "ALTER TABLE estate_plots ADD CONSTRAINT ck_estate_plot_development_status "
        "CHECK (development_status IN ('not_started', 'site_cleared', 'foundation', 'under_construction', 'developed'))"
    )


def downgrade() -> None:
    op.execute("UPDATE estate_plots SET development_status = 'not_started' WHERE development_status IN ('site_cleared', 'foundation', 'under_construction')")
    op.execute("ALTER TABLE estate_plots DROP CONSTRAINT IF EXISTS ck_estate_plot_development_status")
    op.execute(
        "ALTER TABLE estate_plots ADD CONSTRAINT ck_estate_plot_development_status "
        "CHECK (development_status IN ('not_started', 'in_progress', 'developed'))"
    )
    op.execute("ALTER TABLE estate_estates DROP COLUMN ownership_details")
    op.execute("ALTER TABLE estate_estates DROP COLUMN project_owner")
    op.execute("ALTER TABLE estate_estates DROP COLUMN project_reference")
