"""Create LandCheck Estates Phase 0 foundations.

Revision ID: 20260913_0001
Revises:
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260913_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_uid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('active', 'suspended', 'archived')", name="ck_estate_organizations_status"),
        sa.UniqueConstraint("organization_uid", name="uq_estate_organizations_uid"),
        sa.UniqueConstraint("slug", name="uq_estate_organizations_slug"),
    )
    op.create_table(
        "estate_organization_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("role_key", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "role_key IN ('owner', 'manager', 'accounts', 'surveyor', 'field_officer', 'sales', 'viewer')",
            name="ck_estate_members_role",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "subject_type", "subject_id", name="uq_estate_member_subject"),
    )
    op.create_index("idx_estate_members_subject", "estate_organization_members", ["subject_type", "subject_id"])
    op.create_index("idx_estate_members_organization_active", "estate_organization_members", ["organization_id", "is_active"])

    op.create_table(
        "estate_organization_entitlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("limit_value", sa.Integer(), nullable=True),
        sa.Column("updated_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("updated_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("limit_value IS NULL OR limit_value >= 0", name="ck_estate_entitlements_limit"),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "feature_key", name="uq_estate_entitlement_feature"),
    )

    op.create_table(
        "estate_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("actor_subject_type", sa.String(length=64), nullable=True),
        sa.Column("actor_subject_id", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("before_data", sa.JSON(), nullable=True),
        sa.Column("after_data", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_uid", name="uq_estate_audit_event_uid"),
    )
    op.create_index("idx_estate_audit_organization_created", "estate_audit_events", ["organization_id", "created_at"])
    op.create_index("idx_estate_audit_entity", "estate_audit_events", ["organization_id", "entity_type", "entity_id"])

    # Audit rows are append-only at the database layer. Retention must use a reviewed migration,
    # not an application delete path.
    op.execute(
        """
        CREATE FUNCTION estate_reject_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'estate_audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER estate_audit_events_no_update_delete
        BEFORE UPDATE OR DELETE ON estate_audit_events
        FOR EACH ROW EXECUTE FUNCTION estate_reject_audit_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS estate_audit_events_no_update_delete ON estate_audit_events")
    op.execute("DROP FUNCTION IF EXISTS estate_reject_audit_mutation()")
    op.drop_index("idx_estate_audit_entity", table_name="estate_audit_events")
    op.drop_index("idx_estate_audit_organization_created", table_name="estate_audit_events")
    op.drop_table("estate_audit_events")
    op.drop_table("estate_organization_entitlements")
    op.drop_index("idx_estate_members_organization_active", table_name="estate_organization_members")
    op.drop_index("idx_estate_members_subject", table_name="estate_organization_members")
    op.drop_table("estate_organization_members")
    op.drop_table("estate_organizations")
