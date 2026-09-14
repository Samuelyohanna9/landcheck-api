"""Create LandCheck Estates Phase 2 financial controls."""
from alembic import op

revision = "20260913_0003"
down_revision = "20260913_0002"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""CREATE TABLE estate_payments (id SERIAL PRIMARY KEY,payment_uid VARCHAR(36) UNIQUE NOT NULL,organization_id INTEGER NOT NULL REFERENCES estate_organizations(id),allocation_id INTEGER NOT NULL REFERENCES estate_allocations(id),customer_id INTEGER NOT NULL REFERENCES estate_customers(id),plot_id INTEGER NOT NULL REFERENCES estate_plots(id),amount NUMERIC(16,2) NOT NULL CHECK(amount>0),currency VARCHAR(3) NOT NULL DEFAULT 'NGN',payment_date TIMESTAMPTZ NOT NULL,payment_method VARCHAR(80) NOT NULL,reference_no VARCHAR(160),notes TEXT,status VARCHAR(32) NOT NULL CHECK(status IN ('recorded','pending_confirmation','confirmed','voided','reversed')),recorded_by_subject_type VARCHAR(64) NOT NULL,recorded_by_subject_id VARCHAR(128) NOT NULL,confirmed_by_subject_type VARCHAR(64),confirmed_by_subject_id VARCHAR(128),confirmed_at TIMESTAMPTZ,voided_by_subject_type VARCHAR(64),voided_by_subject_id VARCHAR(128),voided_at TIMESTAMPTZ,void_reason TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    op.execute("CREATE UNIQUE INDEX uq_estate_payment_reference ON estate_payments(organization_id,reference_no) WHERE reference_no IS NOT NULL")
    op.execute("CREATE INDEX idx_estate_payments_allocation_status ON estate_payments(allocation_id,status,payment_date)")
    op.execute("""CREATE TABLE estate_documents (id SERIAL PRIMARY KEY,document_uid VARCHAR(36) UNIQUE NOT NULL,organization_id INTEGER NOT NULL REFERENCES estate_organizations(id),object_key VARCHAR(512) UNIQUE NOT NULL,original_filename VARCHAR(255) NOT NULL,mime_type VARCHAR(120) NOT NULL,size_bytes INTEGER NOT NULL CHECK(size_bytes>0),checksum VARCHAR(128),document_type VARCHAR(64) NOT NULL DEFAULT 'other',description TEXT,uploaded_by_subject_type VARCHAR(64) NOT NULL,uploaded_by_subject_id VARCHAR(128) NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    op.execute("CREATE TABLE estate_document_links (id SERIAL PRIMARY KEY,document_id INTEGER NOT NULL REFERENCES estate_documents(id) ON DELETE CASCADE,entity_type VARCHAR(64) NOT NULL,entity_id VARCHAR(128) NOT NULL,UNIQUE(document_id,entity_type,entity_id))")
    op.execute("""CREATE TABLE estate_payment_rules (id SERIAL PRIMARY KEY,organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,rule_key VARCHAR(64) NOT NULL,is_enabled BOOLEAN NOT NULL DEFAULT FALSE,percentage NUMERIC(5,2) CHECK(percentage IS NULL OR (percentage>=0 AND percentage<=100)),description TEXT,UNIQUE(organization_id,rule_key))""")

def downgrade() -> None:
    op.execute("DROP TABLE estate_payment_rules"); op.execute("DROP TABLE estate_document_links"); op.execute("DROP TABLE estate_documents"); op.execute("DROP TABLE estate_payments")
