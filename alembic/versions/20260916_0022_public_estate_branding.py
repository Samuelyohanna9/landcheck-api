"""Add public Estate branding and plot addresses."""

from alembic import op
import sqlalchemy as sa


revision = "20260916_0022"
down_revision = "20260916_0021"
branch_labels = None
depends_on = None


def _has_column(bind, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    columns = (
        ("estate_estates", "public_tagline", sa.Column("public_tagline", sa.String(length=255), nullable=True)),
        ("estate_estates", "public_logo_object_key", sa.Column("public_logo_object_key", sa.String(length=512), nullable=True)),
        ("estate_plots", "public_address", sa.Column("public_address", sa.String(length=255), nullable=True)),
    )
    for table_name, column_name, column in columns:
        if not _has_column(bind, table_name, column_name):
            op.add_column(table_name, column)


def downgrade() -> None:
    op.drop_column("estate_plots", "public_address")
    op.drop_column("estate_estates", "public_logo_object_key")
    op.drop_column("estate_estates", "public_tagline")
