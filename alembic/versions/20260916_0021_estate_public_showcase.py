"""Add public Estate plot showcase and reservation-request leads."""

from alembic import op
import sqlalchemy as sa


revision = "20260916_0021"
down_revision = "20260916_0020"
branch_labels = None
depends_on = None


def _has_column(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(bind, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_check_constraint(bind, table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(
        constraint.get("name") == constraint_name
        for constraint in inspector.get_check_constraints(table_name)
    )


def _has_unique_key(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if any(
        list(constraint.get("column_names") or []) == [column_name]
        for constraint in inspector.get_unique_constraints(table_name)
    ):
        return True
    return any(
        bool(index.get("unique")) and list(index.get("column_names") or []) == [column_name]
        for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    estate_columns = (
        ("public_slug", sa.Column("public_slug", sa.String(length=140), nullable=True)),
        ("public_enabled", sa.Column("public_enabled", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("public_description", sa.Column("public_description", sa.Text(), nullable=True)),
        ("public_contact_phone", sa.Column("public_contact_phone", sa.String(length=64), nullable=True)),
        ("public_show_prices", sa.Column("public_show_prices", sa.Boolean(), nullable=False, server_default=sa.true())),
    )
    for column_name, column in estate_columns:
        if not _has_column(bind, "estate_estates", column_name):
            op.add_column("estate_estates", column)
    if not _has_index(bind, "estate_estates", "uq_estate_estates_public_slug"):
        op.create_index("uq_estate_estates_public_slug", "estate_estates", ["public_slug"], unique=True)

    if not _has_column(bind, "estate_plots", "asking_price"):
        op.add_column("estate_plots", sa.Column("asking_price", sa.Numeric(16, 2), nullable=True))
    if not _has_check_constraint(bind, "estate_plots", "ck_estate_plots_asking_price"):
        op.create_check_constraint(
            "ck_estate_plots_asking_price",
            "estate_plots",
            "asking_price IS NULL OR asking_price > 0",
        )

    if not sa.inspect(bind).has_table("estate_public_reservation_requests"):
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

    bind = op.get_bind()
    if not _has_unique_key(bind, "estate_public_reservation_requests", "request_uid"):
        op.create_unique_constraint(
            "uq_estate_public_reservation_request_uid",
            "estate_public_reservation_requests",
            ["request_uid"],
        )
    if not _has_check_constraint(bind, "estate_public_reservation_requests", "ck_estate_public_reservation_status"):
        op.create_check_constraint(
            "ck_estate_public_reservation_status",
            "estate_public_reservation_requests",
            "status IN ('new', 'contacted', 'converted', 'declined')",
        )
    if not _has_index(bind, "estate_public_reservation_requests", "idx_estate_public_reservation_estate_status"):
        op.create_index(
            "idx_estate_public_reservation_estate_status",
            "estate_public_reservation_requests",
            ["estate_id", "status", "created_at"],
        )
    if not _has_index(bind, "estate_public_reservation_requests", "idx_estate_public_reservation_org_status"):
        op.create_index(
            "idx_estate_public_reservation_org_status",
            "estate_public_reservation_requests",
            ["organization_id", "status", "created_at"],
        )


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
