"""Track review of Survey-backed Estate layout imports."""
from alembic import op

revision = "20260914_0009"
down_revision = "20260914_0008"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""CREATE TABLE estate_import_reviews (id SERIAL PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE, estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE, source_type VARCHAR(32) NOT NULL, survey_georeference_session_id VARCHAR(128), status VARCHAR(32) NOT NULL DEFAULT 'review_required', notes TEXT, created_by_subject_type VARCHAR(64) NOT NULL, created_by_subject_id VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), CONSTRAINT ck_estate_import_source CHECK (source_type IN ('coordinates','csv','gis','cad','raster','pdf')), CONSTRAINT ck_estate_import_status CHECK (status IN ('review_required','approved','rejected')))""")
    op.execute("CREATE INDEX ix_estate_import_reviews_estate_status ON estate_import_reviews (estate_id, status)")

def downgrade() -> None:
    op.execute("DROP TABLE estate_import_reviews")
