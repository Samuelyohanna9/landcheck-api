"""Allow a buyer-initiated "requested" status on live inspection sessions."""

from alembic import op


revision = "20261018_0059"
down_revision = "20261018_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_estate_live_inspection_status", "estate_live_inspection_sessions", type_="check")
    op.create_check_constraint(
        "ck_estate_live_inspection_status",
        "estate_live_inspection_sessions",
        "status IN ('requested', 'scheduled', 'live', 'ended', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_estate_live_inspection_status", "estate_live_inspection_sessions", type_="check")
    op.create_check_constraint(
        "ck_estate_live_inspection_status",
        "estate_live_inspection_sessions",
        "status IN ('scheduled', 'live', 'ended', 'cancelled')",
    )
