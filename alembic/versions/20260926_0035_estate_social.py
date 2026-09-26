"""Estate social posting: connected Facebook/Instagram accounts, scheduled posts, WhatsApp opt-ins and sends."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0035"
down_revision = "20260925_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_social_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("username", sa.String(120), nullable=True),
        sa.Column("linked_page_id", sa.String(64), nullable=True),
        sa.Column("facebook_user_id", sa.String(64), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("connected_by_subject_type", sa.String(64), nullable=True),
        sa.Column("connected_by_subject_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "provider", "external_id", name="uq_estate_social_account"),
        sa.CheckConstraint("provider IN ('facebook', 'instagram')", name="ck_estate_social_provider"),
    )

    op.create_table(
        "estate_social_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("post_uid", sa.String(36), nullable=False, unique=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("template_key", sa.String(40), nullable=False, server_default="custom"),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("image_format", sa.String(16), nullable=False, server_default="post"),
        sa.Column("image_style", sa.String(16), nullable=False, server_default="promo"),
        sa.Column("source_code", sa.String(120), nullable=True),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("created_by_subject_type", sa.String(64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('draft', 'scheduled', 'publishing', 'published', 'partial', 'failed', 'cancelled')", name="ck_estate_social_post_status"),
    )
    op.create_index("ix_estate_social_posts_estate", "estate_social_posts", ["estate_id", "created_at"])
    op.create_index("ix_estate_social_posts_due", "estate_social_posts", ["status", "scheduled_at"])

    op.create_table(
        "estate_marketing_optins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("phone_digits", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False, server_default="whatsapp"),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("source_code", sa.String(120), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("unsubscribe_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("estate_id", "phone_digits", "channel", name="uq_estate_marketing_optin"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_estate_marketing_optin_status"),
    )
    op.create_index("ix_estate_marketing_optins_estate", "estate_marketing_optins", ["estate_id", "status"])

    op.create_table(
        "estate_whatsapp_sends",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("optin_id", sa.Integer(), sa.ForeignKey("estate_marketing_optins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("batch_uid", sa.String(36), nullable=False),
        sa.Column("template_name", sa.String(120), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("provider_message_id", sa.String(120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_by_subject_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_estate_whatsapp_sends_batch", "estate_whatsapp_sends", ["estate_id", "batch_uid"])


def downgrade() -> None:
    op.drop_table("estate_whatsapp_sends")
    op.drop_table("estate_marketing_optins")
    op.drop_table("estate_social_posts")
    op.drop_table("estate_social_accounts")
