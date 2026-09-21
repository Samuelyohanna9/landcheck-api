"""Add marketer contacts and revocable agent workspace links."""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0027"
down_revision = "20260921_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_organization_members", sa.Column("contact_email", sa.String(255), nullable=True))
    op.add_column("estate_organization_members", sa.Column("contact_phone", sa.String(64), nullable=True))
    # Rebuild the role check so existing organizations can use marketer members.
    op.drop_constraint("ck_estate_members_role", "estate_organization_members", type_="check")
    op.create_check_constraint(
        "ck_estate_members_role",
        "estate_organization_members",
        "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'marketer', 'viewer')",
    )
    op.create_table(
        "estate_agent_portal_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("estate_organization_members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_estate_agent_portal_tokens_member", "estate_agent_portal_tokens", ["member_id"])


def downgrade() -> None:
    op.drop_index("ix_estate_agent_portal_tokens_member", table_name="estate_agent_portal_tokens")
    op.drop_table("estate_agent_portal_tokens")
    op.drop_constraint("ck_estate_members_role", "estate_organization_members", type_="check")
    op.create_check_constraint(
        "ck_estate_members_role",
        "estate_organization_members",
        "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'viewer')",
    )
    op.drop_column("estate_organization_members", "contact_phone")
    op.drop_column("estate_organization_members", "contact_email")
