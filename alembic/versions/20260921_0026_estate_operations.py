"""Add Estate operations, buyer portal, and QR attribution records."""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0026"
down_revision = "20260921_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_allocations", sa.Column("next_payment_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_allocations", sa.Column("reservation_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_public_reservation_requests", sa.Column("source_code", sa.String(120), nullable=True))
    op.add_column("estate_public_reservation_requests", sa.Column("source_channel", sa.String(64), nullable=True))
    op.add_column("estate_public_reservation_requests", sa.Column("assigned_agent_subject_type", sa.String(64), nullable=True))
    op.add_column("estate_public_reservation_requests", sa.Column("assigned_agent_subject_id", sa.String(128), nullable=True))
    op.add_column("estate_payments", sa.Column("receipt_number", sa.String(120), nullable=True))
    op.add_column("estate_payments", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("estate_payments", sa.Column("reconciled_by_subject_type", sa.String(64), nullable=True))
    op.add_column("estate_payments", sa.Column("reconciled_by_subject_id", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_estate_payment_receipt_number", "estate_payments", ["receipt_number"])

    op.create_table(
        "estate_payment_inbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
        sa.Column("payment_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payer_name", sa.String(255), nullable=True),
        sa.Column("payer_reference", sa.String(160), nullable=True),
        sa.Column("source", sa.String(80), nullable=False, server_default="manual"),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="unmatched"),
        sa.Column("matched_payment_id", sa.Integer(), sa.ForeignKey("estate_payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("match_notes", sa.Text(), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_by_subject_type", sa.String(64), nullable=True),
        sa.Column("matched_by_subject_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('unmatched', 'matched', 'ignored')", name="ck_estate_payment_inbox_status"),
    )
    op.create_index("ix_estate_payment_inbox_org_status", "estate_payment_inbox", ["organization_id", "status"])

    op.create_table(
        "estate_customer_portal_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("estate_customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "estate_qr_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False, server_default="other"),
        sa.Column("assigned_agent_subject_type", sa.String(64), nullable=True),
        sa.Column("assigned_agent_subject_id", sa.String(128), nullable=True),
        sa.Column("scan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("estate_id", "code", name="uq_estate_qr_campaign_code"),
    )


def downgrade() -> None:
    op.drop_column("estate_allocations", "next_payment_due_at")
    op.drop_column("estate_allocations", "reservation_reminder_sent_at")
    op.drop_table("estate_qr_campaigns")
    op.drop_table("estate_customer_portal_tokens")
    op.drop_index("ix_estate_payment_inbox_org_status", table_name="estate_payment_inbox")
    op.drop_table("estate_payment_inbox")
    op.drop_constraint("uq_estate_payment_receipt_number", "estate_payments", type_="unique")
    op.drop_column("estate_payments", "reconciled_by_subject_id")
    op.drop_column("estate_payments", "reconciled_by_subject_type")
    op.drop_column("estate_payments", "reconciled_at")
    op.drop_column("estate_payments", "receipt_number")
    op.drop_column("estate_public_reservation_requests", "assigned_agent_subject_id")
    op.drop_column("estate_public_reservation_requests", "assigned_agent_subject_type")
    op.drop_column("estate_public_reservation_requests", "source_channel")
    op.drop_column("estate_public_reservation_requests", "source_code")
