"""Create minimal Estate staking tasks."""
from alembic import op

revision = "20260913_0005"
down_revision = "20260913_0004"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""CREATE TABLE estate_staking_tasks (id SERIAL PRIMARY KEY,organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,plot_id INTEGER NOT NULL REFERENCES estate_plots(id) ON DELETE CASCADE,survey_request_id INTEGER NOT NULL REFERENCES estate_survey_requests(id) ON DELETE CASCADE,status VARCHAR(32) NOT NULL CHECK(status IN ('pending','assigned','in_progress','completed','cancelled')),assigned_subject_type VARCHAR(64),assigned_subject_id VARCHAR(128),notes TEXT,completed_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    op.execute("CREATE INDEX idx_estate_staking_tasks_org_status ON estate_staking_tasks(organization_id,status)")
    op.execute("CREATE UNIQUE INDEX uq_estate_active_staking_task_request ON estate_staking_tasks(survey_request_id) WHERE status IN ('pending','assigned','in_progress')")

def downgrade() -> None:
    op.execute("DROP TABLE estate_staking_tasks")
