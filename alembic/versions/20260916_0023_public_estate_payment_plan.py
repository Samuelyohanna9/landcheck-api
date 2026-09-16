"""Add public payment-plan settings and reservation conversion links."""

from alembic import op
import sqlalchemy as sa


revision = "20260916_0023"
down_revision = "20260916_0022"
branch_labels = None
depends_on = None


def _has_column(bind, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def _has_foreign_key(bind, table_name: str, local_column: str, referred_table: str, referred_column: str) -> bool:
    for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
        local_columns = foreign_key.get("constrained_columns") or []
        referred_columns = foreign_key.get("referred_columns") or []
        if local_column in local_columns and referred_table in {foreign_key.get("referred_table")} and referred_column in referred_columns:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    columns = (
        ("estate_estates", "public_payment_plan", sa.Column("public_payment_plan", sa.JSON(), nullable=True)),
        ("estate_public_reservation_requests", "customer_id", sa.Column("customer_id", sa.Integer(), nullable=True)),
        ("estate_public_reservation_requests", "allocation_id", sa.Column("allocation_id", sa.Integer(), nullable=True)),
    )
    for table_name, column_name, column in columns:
        if not _has_column(bind, table_name, column_name):
            op.add_column(table_name, column)
    if not _has_foreign_key(bind, "estate_public_reservation_requests", "customer_id", "estate_customers", "id"):
        op.create_foreign_key(
            "fk_public_reservation_customer",
            "estate_public_reservation_requests",
            "estate_customers",
            ["customer_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_foreign_key(bind, "estate_public_reservation_requests", "allocation_id", "estate_allocations", "id"):
        op.create_foreign_key(
            "fk_public_reservation_allocation",
            "estate_public_reservation_requests",
            "estate_allocations",
            ["allocation_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    names = {foreign_key.get("name") for foreign_key in sa.inspect(bind).get_foreign_keys("estate_public_reservation_requests")}
    if "fk_public_reservation_allocation" in names:
        op.drop_constraint("fk_public_reservation_allocation", "estate_public_reservation_requests", type_="foreignkey")
    if "fk_public_reservation_customer" in names:
        op.drop_constraint("fk_public_reservation_customer", "estate_public_reservation_requests", type_="foreignkey")
    if _has_column(bind, "estate_public_reservation_requests", "allocation_id"):
        op.drop_column("estate_public_reservation_requests", "allocation_id")
    if _has_column(bind, "estate_public_reservation_requests", "customer_id"):
        op.drop_column("estate_public_reservation_requests", "customer_id")
    if _has_column(bind, "estate_estates", "public_payment_plan"):
        op.drop_column("estate_estates", "public_payment_plan")
