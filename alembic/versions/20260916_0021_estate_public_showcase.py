"""Add public Estate plot showcase and reservation-request leads."""

from alembic import op
import sqlalchemy as sa


revision = "20260916_0021"
down_revision = "20260916_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_estates", sa.Column("public_slug", sa.String(length=140), nullable=True))
    op.add_column("estate_estates", sa.Column("public_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("estate_estates", sa.Column("public_description", sa.Text(), nullable=True))
    op.add_column("estate_estates", sa.Column("public_contact_phone", sa.String(length=64), nullable=True))
    op.add_column("estate_estates", sa.Column("public_show_prices", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index("uq_estate_estates_public_slug", "estate_estates", ["public_slug"], unique=True)

    op.add_column("estate_plots", sa.Column("asking_price", sa.Numeric(16, 2), nullable=True))
    op.create_check_constraint("ck_estate_plots_asking_price", "estate_plots", "asking_price IS NULL OR asking_price > 0")

    op.create_table(
        "estate_public_reservation_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("estate_id", sa.Integer(), nullable=False),
        sa.Column("plot_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("staff_notes", sa.Text(), nullable=True),
        sa.Column("contacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["estate_id"], ["estate_estates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plot_id"], ["estate_plots.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("request_uid", name="uq_estate_public_reservation_request_uid"),
        sa.CheckConstraint("status IN ('new', 'contacted', 'converted', 'declined')", name="ck_estate_public_reservation_status"),
    )
    op.create_index("idx_estate_public_reservation_estate_status", "estate_public_reservation_requests", ["estate_id", "status", "created_at"])
    op.create_index("idx_estate_public_reservation_org_status", "estate_public_reservation_requests", ["organization_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_estate_public_reservation_org_status", table_name="estate_public_reservation_requests")
    op.drop_index("idx_estate_public_reservation_estate_status", table_name="estate_public_reservation_requests")
    op.drop_table("estate_public_reservation_requests")
    op.drop_constraint("ck_estate_plots_asking_price", "estate_plots", type_="check")
    op.drop_column("estate_plots", "asking_price")
    op.drop_index("uq_estate_estates_public_slug", table_name="estate_estates")
    op.drop_column("estate_estates", "public_show_prices")
    op.drop_column("estate_estates", "public_contact_phone")
    op.drop_column("estate_estates", "public_description")
    op.drop_column("estate_estates", "public_enabled")
    op.drop_column("estate_estates", "public_slug")
