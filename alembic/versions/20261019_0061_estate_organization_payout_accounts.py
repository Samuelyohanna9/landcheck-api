"""Add estate_organization_payout_accounts - the developer's bank account / gateway subaccount
that buyer online plot payments settle into directly, instead of LandCheck's own account."""

from alembic import op
import sqlalchemy as sa


revision = "20261019_0061"
down_revision = "20261019_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_organization_payout_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("bank_code", sa.String(length=16), nullable=False),
        sa.Column("bank_name", sa.String(length=160), nullable=True),
        sa.Column("account_number", sa.String(length=32), nullable=False),
        sa.Column("account_name", sa.String(length=255), nullable=False),
        sa.Column("paystack_subaccount_code", sa.String(length=64), nullable=True),
        sa.Column("flutterwave_subaccount_id", sa.String(length=64), nullable=True),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", name="uq_estate_organization_payout_accounts_org"),
    )


def downgrade() -> None:
    op.drop_table("estate_organization_payout_accounts")
