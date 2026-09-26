"""Estate social content plans: auto-written, auto-scheduled posts."""

from alembic import op
import sqlalchemy as sa


revision = "20260927_0036"
down_revision = "20260926_0035"
branch_labels = None
depends_on = None

NEW_STATUSES = "status IN ('draft', 'scheduled', 'publishing', 'published', 'partial', 'failed', 'cancelled', 'skipped')"
OLD_STATUSES = "status IN ('draft', 'scheduled', 'publishing', 'published', 'partial', 'failed', 'cancelled')"


def upgrade() -> None:
    op.create_table(
        "estate_social_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("per_day", sa.Integer(), nullable=True),
        sa.Column("per_week", sa.Integer(), nullable=True),
        sa.Column("times", sa.JSON(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("style", sa.String(16), nullable=False, server_default="mixed"),
        sa.Column("tone", sa.String(16), nullable=False, server_default="friendly"),
        sa.Column("duration_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_code", sa.String(120), nullable=True),
        sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'paused', 'ended')", name="ck_estate_social_plan_status"),
    )
    op.create_index("ix_estate_social_plans_estate", "estate_social_plans", ["estate_id", "status"])

    op.add_column("estate_social_posts", sa.Column("plan_id", sa.Integer(), sa.ForeignKey("estate_social_plans.id", ondelete="SET NULL"), nullable=True))
    op.add_column("estate_social_posts", sa.Column("auto_caption", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("estate_social_posts", sa.Column("variant", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_estate_social_posts_plan", "estate_social_posts", ["plan_id", "scheduled_at"])
    op.drop_constraint("ck_estate_social_post_status", "estate_social_posts", type_="check")
    op.create_check_constraint("ck_estate_social_post_status", "estate_social_posts", NEW_STATUSES)


def downgrade() -> None:
    op.drop_constraint("ck_estate_social_post_status", "estate_social_posts", type_="check")
    op.create_check_constraint("ck_estate_social_post_status", "estate_social_posts", OLD_STATUSES)
    op.drop_index("ix_estate_social_posts_plan", table_name="estate_social_posts")
    op.drop_column("estate_social_posts", "variant")
    op.drop_column("estate_social_posts", "auto_caption")
    op.drop_column("estate_social_posts", "plan_id")
    op.drop_table("estate_social_plans")
