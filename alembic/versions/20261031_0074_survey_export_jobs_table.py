"""Create the Survey export queue table when legacy bootstrap tables are absent.

The Survey tables predate Alembic and are still bootstrapped by the application. Revision 0073
could only add worker columns when ``plot_export_jobs`` already existed, which left a fresh
database unable to start the dedicated worker. This additive revision creates the queue table
itself, without requiring the legacy tables to have been bootstrapped first. Existing tables and
records are preserved.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261031_0074"
down_revision = "20261031_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS plot_export_jobs (
                id TEXT PRIMARY KEY,
                export_type TEXT NOT NULL,
                plot_id INTEGER,
                subdivision_batch_id INTEGER,
                owner_user_id BIGINT,
                cache_key TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                file_name TEXT,
                local_path TEXT,
                request_payload JSONB,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                available_at TIMESTAMP DEFAULT NOW(),
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                heartbeat_at TIMESTAMP,
                progress INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT,
                cancel_requested_at TIMESTAMP,
                error_text TEXT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS export_type TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS plot_id INTEGER;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS subdivision_batch_id INTEGER;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS cache_key TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'queued';
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS file_name TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS local_path TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS request_payload JSONB;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER DEFAULT 0;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER DEFAULT 3;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS available_at TIMESTAMP DEFAULT NOW();
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS lease_owner TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS lease_token TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS progress INTEGER DEFAULT 0;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS last_error_code TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS error_text TEXT;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
            ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();

            UPDATE plot_export_jobs
            SET attempt_count = COALESCE(attempt_count, 0),
                max_attempts = COALESCE(max_attempts, 3),
                available_at = COALESCE(available_at, created_at, NOW()),
                progress = COALESCE(progress, 0),
                status = COALESCE(status, 'queued'),
                created_at = COALESCE(created_at, NOW()),
                updated_at = COALESCE(updated_at, NOW())
            WHERE attempt_count IS NULL
               OR max_attempts IS NULL
               OR available_at IS NULL
               OR progress IS NULL
               OR status IS NULL
               OR created_at IS NULL
               OR updated_at IS NULL;

            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_status_created
                ON plot_export_jobs(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_claim
                ON plot_export_jobs(status, available_at, created_at);
            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_lease_expiry
                ON plot_export_jobs(status, lease_expires_at);
            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_owner_status
                ON plot_export_jobs(owner_user_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_batch_created
                ON plot_export_jobs(subdivision_batch_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_type_batch_created
                ON plot_export_jobs(export_type, subdivision_batch_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_plot_cache_created
                ON plot_export_jobs(plot_id, export_type, cache_key, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_plot_export_jobs_unique_live_cache_key
                ON plot_export_jobs(plot_id, export_type, cache_key)
                WHERE cache_key IS NOT NULL AND status <> 'failed';

            DO $$
            BEGIN
                IF to_regclass('public.plots') IS NOT NULL THEN
                    UPDATE plot_export_jobs j
                    SET owner_user_id = p.owner_user_id
                    FROM plots p
                    WHERE j.owner_user_id IS NULL AND p.id = j.plot_id;
                END IF;
                IF to_regclass('public.plot_subdivision_batches') IS NOT NULL
                   AND to_regclass('public.plots') IS NOT NULL THEN
                    UPDATE plot_export_jobs j
                    SET owner_user_id = parent_plot.owner_user_id
                    FROM plot_subdivision_batches b
                    JOIN plots parent_plot ON parent_plot.id = b.parent_plot_id
                    WHERE j.owner_user_id IS NULL AND b.id = j.subdivision_batch_id;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    # Deliberately non-destructive: dropping this table would delete queued jobs and historical
    # export records. A rollback restores the previous application image while retaining the
    # additive queue schema and its data.
    pass
