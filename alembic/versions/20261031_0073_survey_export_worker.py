"""Add durable lease and retry state for the Survey export worker."""

from alembic import op
import sqlalchemy as sa


revision = "20261031_0073"
down_revision = "20261029_0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Survey's legacy tables may be created by the existing bootstrap path after Alembic runs on
    # a fresh database. Upgrade an existing table here; the bootstrap path also contains the same
    # additive columns for that first-start case.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF to_regclass('public.plot_export_jobs') IS NOT NULL THEN
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS available_at TIMESTAMP DEFAULT NOW();
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS lease_owner TEXT;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS lease_token TEXT;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS progress INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS last_error_code TEXT;
                    ALTER TABLE plot_export_jobs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP;

                    UPDATE plot_export_jobs j
                    SET owner_user_id = p.owner_user_id
                    FROM plots p
                    WHERE j.owner_user_id IS NULL
                      AND p.id = j.plot_id;

                    UPDATE plot_export_jobs j
                    SET owner_user_id = parent_plot.owner_user_id
                    FROM plot_subdivision_batches b
                    JOIN plots parent_plot ON parent_plot.id = b.parent_plot_id
                    WHERE j.owner_user_id IS NULL
                      AND b.id = j.subdivision_batch_id;

                    UPDATE plot_export_jobs
                    SET available_at = COALESCE(available_at, created_at, NOW()),
                        max_attempts = COALESCE(max_attempts, 3),
                        attempt_count = COALESCE(attempt_count, 0),
                        progress = COALESCE(progress, 0)
                    WHERE available_at IS NULL
                       OR max_attempts IS NULL
                       OR attempt_count IS NULL
                       OR progress IS NULL;

                    CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_claim
                        ON plot_export_jobs(status, available_at, created_at);
                    CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_lease_expiry
                        ON plot_export_jobs(status, lease_expires_at);
                    CREATE INDEX IF NOT EXISTS idx_plot_export_jobs_owner_status
                        ON plot_export_jobs(owner_user_id, status, created_at);
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF to_regclass('public.plot_export_jobs') IS NOT NULL THEN
                    DROP INDEX IF EXISTS idx_plot_export_jobs_owner_status;
                    DROP INDEX IF EXISTS idx_plot_export_jobs_lease_expiry;
                    DROP INDEX IF EXISTS idx_plot_export_jobs_claim;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS cancel_requested_at;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS last_error_code;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS progress;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS heartbeat_at;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS lease_expires_at;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS lease_token;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS lease_owner;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS available_at;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS max_attempts;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS attempt_count;
                    ALTER TABLE plot_export_jobs DROP COLUMN IF EXISTS owner_user_id;
                END IF;
            END $$;
            """
        )
    )
