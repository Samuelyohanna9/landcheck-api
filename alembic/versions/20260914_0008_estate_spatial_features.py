"""Add Estate roads, drainage, open-space and infrastructure layers."""
from alembic import op

revision = "20260914_0008"
down_revision = "20260914_0007"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""CREATE TABLE estate_spatial_features (id SERIAL PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE, estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE, feature_type VARCHAR(32) NOT NULL, name VARCHAR(160), geometry geometry(GEOMETRY,4326) NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'active', created_by_subject_type VARCHAR(64) NOT NULL, created_by_subject_id VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), CONSTRAINT ck_estate_spatial_feature_type CHECK (feature_type IN ('road','drainage','open_space','infrastructure')))""")
    op.execute("CREATE INDEX ix_estate_spatial_features_estate_type ON estate_spatial_features (estate_id, feature_type)")

def downgrade() -> None:
    op.execute("DROP TABLE estate_spatial_features")
