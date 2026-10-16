"""Estate live video + GPS-verified site inspection sessions."""

from alembic import op
import sqlalchemy as sa


revision = "20261017_0055"
down_revision = "20261011_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_live_inspection_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_uid", sa.String(36), nullable=False, unique=True),
        sa.Column("buyer_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("estate_customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("buyer_name", sa.String(255), nullable=False),
        sa.Column("buyer_phone", sa.String(64), nullable=True),
        sa.Column("buyer_email", sa.String(255), nullable=True),
        sa.Column("agent_member_id", sa.Integer(), sa.ForeignKey("estate_organization_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="scheduled"),
        sa.Column("video_room_name", sa.String(255), nullable=True),
        sa.Column("video_room_url", sa.String(1024), nullable=True),
        sa.Column("last_lat", sa.Float(), nullable=True),
        sa.Column("last_lng", sa.Float(), nullable=True),
        sa.Column("last_accuracy_m", sa.Float(), nullable=True),
        sa.Column("last_verification", sa.String(16), nullable=True),
        sa.Column("last_distance_m", sa.Float(), nullable=True),
        sa.Column("last_location_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('scheduled', 'live', 'ended', 'cancelled')", name="ck_estate_live_inspection_status"),
        sa.CheckConstraint(
            "last_verification IS NULL OR last_verification IN ('on_plot', 'on_estate', 'near_estate', 'outside')",
            name="ck_estate_live_inspection_verification",
        ),
    )
    op.create_index("ix_estate_live_inspections_estate_status", "estate_live_inspection_sessions", ["estate_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_estate_live_inspections_estate_status", table_name="estate_live_inspection_sessions")
    op.drop_table("estate_live_inspection_sessions")
