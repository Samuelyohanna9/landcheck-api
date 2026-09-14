"""Add parcel-scoped field inspection records."""

from alembic import op


revision = "20260914_0007"
down_revision = "20260914_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE estate_field_inspections (
        id SERIAL PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,
        estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,
        plot_id INTEGER NOT NULL REFERENCES estate_plots(id) ON DELETE CASCADE,
        inspection_type VARCHAR(64) NOT NULL DEFAULT 'site_visit', outcome VARCHAR(32) NOT NULL DEFAULT 'observed',
        notes TEXT NULL, inspected_by_subject_type VARCHAR(64) NOT NULL, inspected_by_subject_id VARCHAR(128) NOT NULL,
        inspected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_estate_field_inspection_outcome CHECK (outcome IN ('observed','attention_required','passed','failed'))
    )""")
    op.execute("CREATE INDEX ix_estate_field_inspections_plot_time ON estate_field_inspections (plot_id, inspected_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE estate_field_inspections")
