"""Version optimization-specific idempotency and export-job schema changes."""

from alembic import op
import sqlalchemy as sa


revision = "20261025_0068"
down_revision = "20261023_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Green mutation retries need durable storage before the API can safely acknowledge a
    # retried field mutation. This table has no foreign keys so it can be migrated independently
    # of the legacy Green bootstrap tables.
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS green_mutation_idempotency (
                scope_key TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_body JSONB,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (scope_key, idempotency_key)
            )
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_green_mutation_idempotency_created "
            "ON green_mutation_idempotency(created_at)"
        )
    )

    # Survey plot tables predate Alembic and are still bootstrapped by the legacy Survey path.
    # Upgrade them when present without preventing a fresh database from completing its Estate
    # migration chain before the application bootstrap creates those legacy tables.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF to_regclass('public.plots') IS NOT NULL THEN
                    ALTER TABLE plots ADD COLUMN IF NOT EXISTS client_request_id TEXT;
                    ALTER TABLE plots ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
                    IF EXISTS (
                        SELECT 1
                        FROM plots
                        WHERE client_request_id IS NOT NULL
                        GROUP BY client_request_id
                        HAVING COUNT(*) > 1
                    ) THEN
                        RAISE EXCEPTION 'Duplicate plots.client_request_id values prevent idempotency index creation';
                    END IF;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_plots_client_request_id
                        ON plots (client_request_id) WHERE client_request_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS idx_plots_owner_user_id
                        ON plots (owner_user_id);
                END IF;

                IF to_regclass('public.plot_feature_overrides') IS NOT NULL THEN
                    ALTER TABLE plot_feature_overrides ADD COLUMN IF NOT EXISTS client_request_id TEXT;
                    IF EXISTS (
                        SELECT 1
                        FROM plot_feature_overrides
                        WHERE client_request_id IS NOT NULL
                        GROUP BY client_request_id
                        HAVING COUNT(*) > 1
                    ) THEN
                        RAISE EXCEPTION 'Duplicate plot_feature_overrides.client_request_id values prevent idempotency index creation';
                    END IF;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_plot_feature_overrides_client_request_id
                        ON plot_feature_overrides (client_request_id)
                        WHERE client_request_id IS NOT NULL;
                END IF;

                IF to_regclass('public.plot_export_jobs') IS NOT NULL THEN
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS cache_key TEXT;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS request_payload JSONB;
                    IF EXISTS (
                        SELECT 1
                        FROM plot_export_jobs
                        WHERE cache_key IS NOT NULL AND status <> 'failed'
                        GROUP BY plot_id, export_type, cache_key
                        HAVING COUNT(*) > 1
                    ) THEN
                        RAISE EXCEPTION 'Duplicate live plot_export_jobs cache keys prevent unique index creation';
                    END IF;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_plot_export_jobs_unique_live_cache_key
                        ON plot_export_jobs(plot_id, export_type, cache_key)
                        WHERE cache_key IS NOT NULL AND status <> 'failed';
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    # The legacy Survey tables remain owned by their bootstrap path. Only optimization-owned
    # objects are removed on downgrade; existing plot columns and indexes are intentionally kept.
    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS idx_green_mutation_idempotency_created"
        )
    )
    op.execute(sa.text("DROP TABLE IF EXISTS green_mutation_idempotency"))
