"""Add independent development tracking to Estate plots."""

from alembic import op


revision = "20260914_0006"
down_revision = "20260913_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_plots ADD COLUMN development_status VARCHAR(32) NOT NULL DEFAULT 'not_started'")
    op.execute("ALTER TABLE estate_plots ADD CONSTRAINT ck_estate_plot_development_status CHECK (development_status IN ('not_started', 'in_progress', 'developed'))")
    op.execute("CREATE INDEX ix_estate_plots_development_status ON estate_plots (estate_id, development_status)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_estate_plots_development_status")
    op.execute("ALTER TABLE estate_plots DROP CONSTRAINT IF EXISTS ck_estate_plot_development_status")
    op.execute("ALTER TABLE estate_plots DROP COLUMN development_status")
