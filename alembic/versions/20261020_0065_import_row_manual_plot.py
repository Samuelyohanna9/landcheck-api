"""Add EstateImportRow.manual_plot_id, so staff can manually link an import row to an existing
plot when the spreadsheet's plot text didn't auto-match one (an escape hatch alongside the
existing automatic plot_number matching in data_import.py)."""

from alembic import op
import sqlalchemy as sa


revision = "20261020_0065"
down_revision = "20261020_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_import_rows", sa.Column("manual_plot_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_estate_import_rows_manual_plot_id",
        "estate_import_rows",
        "estate_plots",
        ["manual_plot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_estate_import_rows_manual_plot_id", "estate_import_rows", type_="foreignkey")
    op.drop_column("estate_import_rows", "manual_plot_id")
