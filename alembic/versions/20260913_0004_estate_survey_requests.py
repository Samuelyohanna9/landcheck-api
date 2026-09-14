"""Create Estate Survey request linkage."""
from alembic import op

revision = "20260913_0004"
down_revision = "20260913_0003"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""CREATE TABLE estate_survey_requests (id SERIAL PRIMARY KEY,request_uid VARCHAR(36) UNIQUE NOT NULL,organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,plot_id INTEGER NOT NULL REFERENCES estate_plots(id) ON DELETE CASCADE,allocation_id INTEGER REFERENCES estate_allocations(id) ON DELETE SET NULL,requested_by_subject_type VARCHAR(64) NOT NULL,requested_by_subject_id VARCHAR(128) NOT NULL,assigned_surveyor_subject_type VARCHAR(64),assigned_surveyor_subject_id VARCHAR(128),status VARCHAR(32) NOT NULL CHECK(status IN ('requested','assigned','in_progress','ready_for_review','approved','completed','cancelled','failed')),survey_owner_user_id INTEGER,survey_working_plot_id INTEGER UNIQUE,survey_reference VARCHAR(128),materialized_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    op.execute("CREATE INDEX idx_estate_survey_requests_org_status ON estate_survey_requests(organization_id,status)")
    op.execute("CREATE INDEX idx_estate_survey_requests_estate_plot ON estate_survey_requests(estate_id,plot_id)")
    op.execute("CREATE UNIQUE INDEX uq_estate_active_survey_request_plot ON estate_survey_requests(plot_id) WHERE status IN ('requested','assigned','in_progress','ready_for_review','approved','failed')")

def downgrade() -> None:
    op.execute("DROP TABLE estate_survey_requests")
