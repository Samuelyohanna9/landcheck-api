"""Add the "Connect your tools" Excel/CSV import feature: estate_import_batches,
estate_import_rows, estate_import_mapping_presets, plus external_id on estate_customers and
external_reference on estate_allocations for repeat-import matching."""

from alembic import op
import sqlalchemy as sa


revision = "20261020_0064"
down_revision = "20261019_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("estate_customers", sa.Column("external_id", sa.String(length=128), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX uq_estate_customers_external_id "
        "ON estate_customers (organization_id, external_id) "
        "WHERE external_id IS NOT NULL"
    )
    op.add_column("estate_allocations", sa.Column("external_reference", sa.String(length=160), nullable=True))

    op.create_table(
        "estate_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_uid", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("estate_id", sa.Integer(), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="uploaded"),
        sa.Column("column_mapping", sa.JSON(), nullable=True),
        sa.Column("opening_balance_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_counts", sa.JSON(), nullable=True),
        sa.Column("uploaded_by_subject_type", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by_subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_by_subject_type", sa.String(length=64), nullable=True),
        sa.Column("reversed_by_subject_id", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["estate_id"], ["estate_estates.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("batch_uid", name="uq_estate_import_batches_uid"),
        sa.CheckConstraint("status IN ('uploaded', 'previewed', 'committed', 'reversed')", name="ck_estate_import_batches_status"),
    )
    op.create_index("idx_estate_import_batches_org", "estate_import_batches", ["organization_id"])

    op.create_table(
        "estate_import_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("source_data", sa.JSON(), nullable=False),
        sa.Column("match_status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("matched_entity_type", sa.String(length=32), nullable=True),
        sa.Column("matched_entity_id", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.String(length=16), nullable=False, server_default="create"),
        sa.Column("issues", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("committed_customer_id", sa.Integer(), nullable=True),
        sa.Column("committed_allocation_id", sa.Integer(), nullable=True),
        sa.Column("committed_payment_id", sa.Integer(), nullable=True),
        sa.Column("created_new_customer", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_new_allocation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["batch_id"], ["estate_import_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["committed_customer_id"], ["estate_customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["committed_allocation_id"], ["estate_allocations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["committed_payment_id"], ["estate_payments.id"], ondelete="SET NULL"),
        sa.CheckConstraint("match_status IN ('new', 'matched', 'ambiguous', 'conflict', 'error')", name="ck_estate_import_rows_match_status"),
        sa.CheckConstraint("resolution IN ('create', 'update', 'skip')", name="ck_estate_import_rows_resolution"),
    )
    op.create_index("idx_estate_import_rows_batch", "estate_import_rows", ["batch_id"])

    op.create_table(
        "estate_import_mapping_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["organization_id"], ["estate_organizations.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_estate_import_mapping_presets_org", "estate_import_mapping_presets", ["organization_id"])


def downgrade() -> None:
    op.drop_index("idx_estate_import_mapping_presets_org", table_name="estate_import_mapping_presets")
    op.drop_table("estate_import_mapping_presets")
    op.drop_index("idx_estate_import_rows_batch", table_name="estate_import_rows")
    op.drop_table("estate_import_rows")
    op.drop_index("idx_estate_import_batches_org", table_name="estate_import_batches")
    op.drop_table("estate_import_batches")
    op.drop_column("estate_allocations", "external_reference")
    op.drop_index("uq_estate_customers_external_id", table_name="estate_customers")
    op.drop_column("estate_customers", "external_id")
