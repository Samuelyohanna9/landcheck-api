"""Estate companies' Data Processing Agreement acceptance record."""

from alembic import op
import sqlalchemy as sa


revision = "20260930_0039"
down_revision = "20260929_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_dpa_acceptances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("estate_organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("accepted_by_subject_type", sa.String(64), nullable=False),
        sa.Column("accepted_by_subject_id", sa.String(128), nullable=False),
        sa.Column("accepted_by_name", sa.String(255), nullable=True),
        sa.Column("accepted_by_email", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_estate_dpa_acceptances_org", "estate_dpa_acceptances", ["organization_id", "accepted_at"])


def downgrade() -> None:
    op.drop_index("ix_estate_dpa_acceptances_org", table_name="estate_dpa_acceptances")
    op.drop_table("estate_dpa_acceptances")
