"""Add independent Estate company accounts and bearer sessions."""

from alembic import op
import sqlalchemy as sa


revision = "20260914_0014"
down_revision = "20260914_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("email_normalized", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('active', 'suspended', 'archived')", name="ck_estate_accounts_status"),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("account_uid", name="uq_estate_accounts_uid"),
        sa.UniqueConstraint("email_normalized", name="uq_estate_accounts_email_normalized"),
        sa.UniqueConstraint("organization_id", "email_normalized", name="uq_estate_account_org_email"),
    )
    op.create_index("idx_estate_accounts_organization", "estate_accounts", ["organization_id"])
    op.create_table(
        "estate_auth_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_uid", sa.String(length=64), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("session_state", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("client_label", sa.String(length=120), nullable=True),
        sa.Column("ip_address", sa.String(length=255), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        sa.CheckConstraint("session_state IN ('active', 'revoked', 'expired')", name="ck_estate_auth_sessions_state"),
        sa.ForeignKeyConstraint(["account_id"], ["estate_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("session_uid", name="uq_estate_auth_session_uid"),
        sa.UniqueConstraint("access_token_hash", name="uq_estate_auth_session_token"),
    )
    op.create_index("idx_estate_auth_sessions_account", "estate_auth_sessions", ["account_id"])
    op.create_index("idx_estate_auth_sessions_organization", "estate_auth_sessions", ["organization_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_auth_sessions_organization", table_name="estate_auth_sessions")
    op.drop_index("idx_estate_auth_sessions_account", table_name="estate_auth_sessions")
    op.drop_table("estate_auth_sessions")
    op.drop_index("idx_estate_accounts_organization", table_name="estate_accounts")
    op.drop_table("estate_accounts")
