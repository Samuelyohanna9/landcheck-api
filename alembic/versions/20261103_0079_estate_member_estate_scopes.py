"""Add opt-in per-estate scoping for Estate organization members (e.g. restricting an Architect
collaborator to the one estate they're working on, without touching anyone else's unrestricted
access)."""

from alembic import op
import sqlalchemy as sa


revision = "20261103_0079"
down_revision = "20261103_0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_member_estate_scopes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("estate_organization_members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("member_id", "estate_id", name="uq_estate_member_estate_scope"),
    )
    op.create_index("ix_estate_member_estate_scopes_member", "estate_member_estate_scopes", ["member_id"])


def downgrade() -> None:
    op.drop_table("estate_member_estate_scopes")
