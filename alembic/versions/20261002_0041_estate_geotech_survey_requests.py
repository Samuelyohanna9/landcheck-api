"""Geotechnical survey request - staff-initiated handoff from the Estate dashboard's Ground &
Drainage screening (which is explicitly not a soil test) to a real geotechnical investigation."""

from alembic import op
import sqlalchemy as sa


revision = "20261002_0041"
down_revision = "20261001_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_geotech_survey_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_uid", sa.String(36), nullable=False, unique=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("estate_id", sa.Integer(), sa.ForeignKey("estate_estates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_id", sa.Integer(), sa.ForeignKey("estate_plots.id", ondelete="CASCADE"), nullable=True),
        sa.Column("requested_by_subject_type", sa.String(64), nullable=False),
        sa.Column("requested_by_subject_id", sa.String(128), nullable=False),
        sa.Column("contact_name", sa.String(255), nullable=True),
        sa.Column("contact_phone", sa.String(64), nullable=True),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('new', 'contacted', 'completed', 'declined')", name="ck_geotech_survey_request_status"),
    )
    op.create_index("ix_estate_geotech_survey_requests_org", "estate_geotech_survey_requests", ["organization_id", "created_at"])
    op.create_index("ix_estate_geotech_survey_requests_estate", "estate_geotech_survey_requests", ["estate_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_estate_geotech_survey_requests_estate", table_name="estate_geotech_survey_requests")
    op.drop_index("ix_estate_geotech_survey_requests_org", table_name="estate_geotech_survey_requests")
    op.drop_table("estate_geotech_survey_requests")
