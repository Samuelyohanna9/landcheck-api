"""Store non-operational CSV import candidates for review."""
from alembic import op

revision = "20260914_0010"
down_revision = "20260914_0009"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("ALTER TABLE estate_import_reviews ADD COLUMN candidate_data JSON NOT NULL DEFAULT '[]'")

def downgrade() -> None:
    op.execute("ALTER TABLE estate_import_reviews DROP COLUMN candidate_data")
