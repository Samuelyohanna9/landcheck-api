"""Require email verification for new Estate company registrations."""

from alembic import op
import sqlalchemy as sa


revision = "20261005_0044"
down_revision = "20261004_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_accounts", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    # Existing accounts were created before verification existed. Preserve their current access;
    # only accounts created after this migration must complete the email link flow.
    op.execute("UPDATE estate_accounts SET email_verified_at = COALESCE(created_at, NOW()) WHERE email_verified_at IS NULL")
    op.create_table(
        "estate_email_verification_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("estate_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_estate_email_verification_tokens_account",
        "estate_email_verification_tokens",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_estate_email_verification_tokens_account", table_name="estate_email_verification_tokens")
    op.drop_table("estate_email_verification_tokens")
    op.drop_column("estate_accounts", "email_verified_at")
