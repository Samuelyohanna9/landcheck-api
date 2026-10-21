"""Add estate_settlement_requests (the "request immediate settlement" feature) and the manual-
transfer tracking columns on estate_allocation_charge_attempts the Estates admin accounting view
(app/routers/estate_admin_accounting.py) writes to when a LandCheck admin manually triggers a
Flutterwave transfer ahead of the normal automatic subaccount settlement."""

from alembic import op
import sqlalchemy as sa


revision = "20261021_0066"
down_revision = "20261020_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_allocation_charge_attempts", sa.Column("settlement_channel", sa.String(length=24), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("manual_transfer_id", sa.String(length=120), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("manual_transfer_reference", sa.String(length=160), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("manual_transfer_status", sa.String(length=32), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("manual_transfer_error", sa.Text(), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("manual_settled_by_subject_type", sa.String(length=64), nullable=True))
    op.add_column("estate_allocation_charge_attempts", sa.Column("manual_settled_by_subject_id", sa.String(length=128), nullable=True))

    op.create_table(
        "estate_settlement_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("charge_attempt_id", sa.Integer(), nullable=True),
        sa.Column("amount_snapshot", sa.Numeric(16, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="requested"),
        sa.Column("requested_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("requested_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["charge_attempt_id"], ["estate_allocation_charge_attempts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("request_uid", name="uq_estate_settlement_requests_uid"),
        sa.CheckConstraint("status IN ('requested', 'in_review', 'settled', 'declined')", name="ck_estate_settlement_requests_status"),
    )
    op.create_index("idx_estate_settlement_requests_org", "estate_settlement_requests", ["organization_id"])
    op.create_index("idx_estate_settlement_requests_status", "estate_settlement_requests", ["status"])


def downgrade() -> None:
    op.drop_index("idx_estate_settlement_requests_status", table_name="estate_settlement_requests")
    op.drop_index("idx_estate_settlement_requests_org", table_name="estate_settlement_requests")
    op.drop_table("estate_settlement_requests")
    op.drop_column("estate_allocation_charge_attempts", "manual_settled_by_subject_id")
    op.drop_column("estate_allocation_charge_attempts", "manual_settled_by_subject_type")
    op.drop_column("estate_allocation_charge_attempts", "manual_transfer_error")
    op.drop_column("estate_allocation_charge_attempts", "manual_transfer_status")
    op.drop_column("estate_allocation_charge_attempts", "manual_transfer_reference")
    op.drop_column("estate_allocation_charge_attempts", "manual_transfer_id")
    op.drop_column("estate_allocation_charge_attempts", "settlement_channel")
