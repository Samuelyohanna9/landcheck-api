"""Add Access: custom staff roles + per-member custom permissions + forced first-login password
change for staff dashboard accounts."""

from alembic import op
import sqlalchemy as sa


revision = "20261004_0043"
down_revision = "20261003_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_staff_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "name", name="uq_estate_staff_role_org_name"),
    )

    op.add_column("estate_organization_members", sa.Column("custom_role_id", sa.Integer(), sa.ForeignKey("estate_staff_roles.id", ondelete="SET NULL"), nullable=True))
    op.add_column("estate_organization_members", sa.Column("custom_permissions", sa.JSON(), nullable=True))

    op.drop_constraint("ck_estate_members_role", "estate_organization_members", type_="check")
    op.create_check_constraint(
        "ck_estate_members_role",
        "estate_organization_members",
        "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'marketer', 'viewer', 'staff')",
    )

    op.add_column("estate_accounts", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("estate_accounts", "must_change_password")

    op.drop_constraint("ck_estate_members_role", "estate_organization_members", type_="check")
    op.create_check_constraint(
        "ck_estate_members_role",
        "estate_organization_members",
        "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'marketer', 'viewer')",
    )

    op.drop_column("estate_organization_members", "custom_permissions")
    op.drop_column("estate_organization_members", "custom_role_id")

    op.drop_table("estate_staff_roles")
