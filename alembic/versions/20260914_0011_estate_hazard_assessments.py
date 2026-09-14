"""Persist parcel hazard assessment results for Estate dashboards."""

from alembic import op


revision = "20260914_0011"
down_revision = "20260914_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE estate_hazard_assessments (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,
        estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,
        plot_id INTEGER REFERENCES estate_plots(id) ON DELETE CASCADE,
        hazard_type VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'completed',
        risk_class VARCHAR(64),
        risk_score NUMERIC(8,3),
        result_payload JSON NOT NULL DEFAULT '{}',
        source VARCHAR(80) NOT NULL DEFAULT 'landcheck_hazard_engine',
        assessed_by_subject_type VARCHAR(64) NOT NULL,
        assessed_by_subject_id VARCHAR(128) NOT NULL,
        assessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_estate_hazard_type CHECK (hazard_type IN ('flood','erosion','lulc')),
        CONSTRAINT ck_estate_hazard_status CHECK (status IN ('completed','failed')),
        CONSTRAINT ck_estate_hazard_score CHECK (risk_score IS NULL OR risk_score >= 0)
    )""")
    op.execute("CREATE INDEX ix_estate_hazard_assessments_estate_plot ON estate_hazard_assessments (estate_id, plot_id, hazard_type, assessed_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE estate_hazard_assessments")
