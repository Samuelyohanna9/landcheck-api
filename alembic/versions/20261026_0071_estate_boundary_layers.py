"""Allow additional Estate boundary layers."""

from alembic import op
from sqlalchemy import text


revision = "20261026_0071"
down_revision = "20261025_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE estate_spatial_features DROP CONSTRAINT IF EXISTS ck_estate_spatial_feature_type")
    op.execute(
        """
        ALTER TABLE estate_spatial_features
        ADD CONSTRAINT ck_estate_spatial_feature_type
        CHECK (feature_type IN ('boundary', 'road', 'drainage', 'open_space', 'infrastructure'))
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SELECT 1 FROM estate_spatial_features WHERE feature_type = 'boundary' LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while Estate boundary layers exist; archive or migrate them first.")
    op.execute("ALTER TABLE estate_spatial_features DROP CONSTRAINT IF EXISTS ck_estate_spatial_feature_type")
    op.execute(
        """
        ALTER TABLE estate_spatial_features
        ADD CONSTRAINT ck_estate_spatial_feature_type
        CHECK (feature_type IN ('road', 'drainage', 'open_space', 'infrastructure'))
        """
    )
