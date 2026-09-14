"""Create LandCheck Estates Phase 1 core entities."""

from alembic import op
import sqlalchemy as sa


revision = "20260913_0002"
down_revision = "20260913_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("""
        CREATE TABLE estate_estates (
          id SERIAL PRIMARY KEY, estate_uid VARCHAR(36) NOT NULL UNIQUE,
          organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,
          name VARCHAR(255) NOT NULL, description TEXT, state VARCHAR(120), locality VARCHAR(160), location_text VARCHAR(255),
          status VARCHAR(32) NOT NULL DEFAULT 'planning' CHECK (status IN ('planning','active','selling','fully_allocated','completed','archived')),
          crs VARCHAR(80) NOT NULL DEFAULT 'EPSG:4326', datum VARCHAR(80), approximate_area_sqm NUMERIC(16,2),
          boundary geometry(POLYGON,4326), created_by_subject_type VARCHAR(64) NOT NULL, created_by_subject_id VARCHAR(128) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), archived_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX idx_estates_org_status ON estate_estates(organization_id,status)")
    op.execute("CREATE INDEX idx_estates_boundary_gist ON estate_estates USING GIST(boundary)")
    op.execute("""
        CREATE TABLE estate_blocks (
          id SERIAL PRIMARY KEY, estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,
          label VARCHAR(80) NOT NULL, name VARCHAR(160), geometry geometry(POLYGON,4326), notes TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(estate_id,label)
        )
    """)
    op.execute("CREATE INDEX idx_blocks_geometry_gist ON estate_blocks USING GIST(geometry)")
    op.execute("""
        CREATE TABLE estate_plots (
          id SERIAL PRIMARY KEY, plot_uid VARCHAR(36) NOT NULL UNIQUE,
          estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,
          block_id INTEGER REFERENCES estate_blocks(id) ON DELETE SET NULL,
          plot_number VARCHAR(120) NOT NULL, plot_number_normalized VARCHAR(120) NOT NULL,
          geometry geometry(POLYGON,4326) NOT NULL, area_sqm NUMERIC(16,2) NOT NULL,
          land_use VARCHAR(120), commercial_status VARCHAR(32) NOT NULL DEFAULT 'available'
            CHECK (commercial_status IN ('available','reserved','allocated','on_hold','disputed','review','sold','staked','developing','developed')),
          geometry_status VARCHAR(32) NOT NULL DEFAULT 'draft' CHECK (geometry_status IN ('draft','review','approved','rejected')),
          source_type VARCHAR(32) NOT NULL DEFAULT 'manual', source_reference VARCHAR(128),
          created_by_subject_type VARCHAR(64) NOT NULL, created_by_subject_id VARCHAR(128) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(estate_id,plot_number_normalized)
        )
    """)
    op.execute("CREATE INDEX idx_plots_estate_status ON estate_plots(estate_id,commercial_status,geometry_status)")
    op.execute("CREATE INDEX idx_plots_geometry_gist ON estate_plots USING GIST(geometry)")
    op.execute("""
        CREATE TABLE estate_customers (
          id SERIAL PRIMARY KEY, customer_uid VARCHAR(36) NOT NULL UNIQUE,
          organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,
          reference_no VARCHAR(80), full_name VARCHAR(255) NOT NULL, full_name_normalized VARCHAR(255) NOT NULL,
          phone VARCHAR(64), email VARCHAR(255), address TEXT, company_name VARCHAR(255), notes TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX idx_customers_org_name ON estate_customers(organization_id,full_name_normalized)")
    op.execute("""
        CREATE TABLE estate_allocations (
          id SERIAL PRIMARY KEY, allocation_uid VARCHAR(36) NOT NULL UNIQUE,
          organization_id INTEGER NOT NULL REFERENCES estate_organizations(id) ON DELETE CASCADE,
          estate_id INTEGER NOT NULL REFERENCES estate_estates(id) ON DELETE CASCADE,
          plot_id INTEGER NOT NULL REFERENCES estate_plots(id) ON DELETE RESTRICT,
          customer_id INTEGER NOT NULL REFERENCES estate_customers(id) ON DELETE RESTRICT,
          status VARCHAR(32) NOT NULL CHECK(status IN ('reserved','allocated','cancelled','expired','released')),
          reservation_date TIMESTAMPTZ, reservation_expires_at TIMESTAMPTZ, allocation_date TIMESTAMPTZ,
          agreed_price NUMERIC(16,2), notes TEXT, cancelled_at TIMESTAMPTZ, cancellation_reason TEXT,
          created_by_subject_type VARCHAR(64) NOT NULL, created_by_subject_id VARCHAR(128) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE UNIQUE INDEX uq_active_estate_allocation_plot ON estate_allocations(plot_id) WHERE status IN ('reserved','allocated')")
    op.execute("CREATE INDEX idx_allocations_org_customer ON estate_allocations(organization_id,customer_id,status)")


def downgrade() -> None:
    op.execute("DROP TABLE estate_allocations")
    op.execute("DROP TABLE estate_customers")
    op.execute("DROP TABLE estate_plots")
    op.execute("DROP TABLE estate_blocks")
    op.execute("DROP TABLE estate_estates")
