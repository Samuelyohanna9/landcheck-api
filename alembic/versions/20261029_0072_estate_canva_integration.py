"""Add organization-scoped Estate Canva connections and design imports."""

from alembic import op
import sqlalchemy as sa


revision = "20261029_0072"
down_revision = "20261026_0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_canva_oauth_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier_enc", sa.Text(), nullable=False),
        sa.Column("return_path", sa.String(length=500), nullable=False),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("state_hash", name="uq_estate_canva_oauth_state_hash"),
    )
    op.create_index("ix_estate_canva_oauth_states_expiry", "estate_canva_oauth_states", ["expires_at", "used_at"])

    op.create_table(
        "estate_canva_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canva_user_id", sa.String(length=128), nullable=False),
        sa.Column("canva_team_id", sa.String(length=128), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("connected_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("connected_by_subject_id", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "canva_user_id", "canva_team_id", name="uq_estate_canva_connection"),
    )
    op.create_index("ix_estate_canva_connections_org_status", "estate_canva_connections", ["organization_id", "status"])

    op.create_table(
        "estate_canva_designs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("estate_canva_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canva_design_id", sa.String(length=128), nullable=False),
        sa.Column("canva_user_id", sa.String(length=128), nullable=False),
        sa.Column("canva_team_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_format", sa.String(length=16), server_default="post", nullable=False),
        sa.Column("source_style", sa.String(length=32), server_default="promo", nullable=False),
        sa.Column("source_plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("return_key_hash", sa.String(length=64), nullable=False),
        sa.Column("r2_object_key", sa.String(length=500), nullable=True),
        sa.Column("r2_content_type", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="created", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_return_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("return_key_hash", name="uq_estate_canva_design_return_key"),
    )
    op.create_index("ix_estate_canva_designs_estate_status", "estate_canva_designs", ["estate_id", "status", "created_at"])
    op.create_index("ix_estate_canva_designs_canva_design", "estate_canva_designs", ["connection_id", "canva_design_id"])


def downgrade() -> None:
    op.drop_index("ix_estate_canva_designs_canva_design", table_name="estate_canva_designs")
    op.drop_index("ix_estate_canva_designs_estate_status", table_name="estate_canva_designs")
    op.drop_table("estate_canva_designs")
    op.drop_index("ix_estate_canva_connections_org_status", table_name="estate_canva_connections")
    op.drop_table("estate_canva_connections")
    op.drop_index("ix_estate_canva_oauth_states_expiry", table_name="estate_canva_oauth_states")
    op.drop_table("estate_canva_oauth_states")
