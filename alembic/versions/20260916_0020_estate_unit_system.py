"""Add a per-Estate unit_system preference ("m" | "ft") for displaying/entering dimensions and areas."""

from alembic import op
import sqlalchemy as sa


revision = "20260916_0020"
down_revision = "20260915_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_estates ADD COLUMN unit_system VARCHAR(4) NOT NULL DEFAULT 'm'")


def downgrade() -> None:
    op.execute("ALTER TABLE estate_estates DROP COLUMN unit_system")
