"""Add a cross-worker rate-limit fingerprint for public demo requests."""

from alembic import op
import sqlalchemy as sa


revision = "20261025_0070"
down_revision = "20261025_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "estate_demo_requests",
        sa.Column("client_fingerprint", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_estate_demo_requests_fingerprint_created_at",
        "estate_demo_requests",
        ["client_fingerprint", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_estate_demo_requests_fingerprint_created_at",
        table_name="estate_demo_requests",
    )
    op.drop_column("estate_demo_requests", "client_fingerprint")
