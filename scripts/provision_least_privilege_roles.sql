-- LandCheck Release B role layout.
--
-- Run this file only as a reviewed PostgreSQL administrator against an isolated staging database
-- first.  It intentionally does not contain passwords.  Set LOGIN and passwords separately using
-- an interactive, secret-safe procedure after the grants and ownership inventory are approved.
-- It is not an Alembic migration and must never run from an API or worker container.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landcheck_migrator') THEN
        CREATE ROLE landcheck_migrator NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    ELSE
        ALTER ROLE landcheck_migrator NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landcheck_api') THEN
        CREATE ROLE landcheck_api NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    ELSE
        ALTER ROLE landcheck_api NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landcheck_survey_worker') THEN
        CREATE ROLE landcheck_survey_worker NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    ELSE
        ALTER ROLE landcheck_survey_worker NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landcheck_maintenance') THEN
        CREATE ROLE landcheck_maintenance NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    ELSE
        ALTER ROLE landcheck_maintenance NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landcheck_superadmin') THEN
        CREATE ROLE landcheck_superadmin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    ELSE
        ALTER ROLE landcheck_superadmin NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO landcheck_api, landcheck_survey_worker;
GRANT CREATE ON SCHEMA public TO landcheck_migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO landcheck_api, landcheck_survey_worker;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO landcheck_api, landcheck_survey_worker;

-- New Alembic-created objects belong to the migration role.  These defaults are applied by the
-- administrator for future objects created while connected as landcheck_migrator.
ALTER DEFAULT PRIVILEGES FOR ROLE landcheck_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO landcheck_api, landcheck_survey_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE landcheck_migrator IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO landcheck_api, landcheck_survey_worker;

-- Alembic's metadata table may predate the dedicated migrator and therefore remain owned by
-- the legacy administrator.  The migrator needs only version-table DML to advance the release;
-- it does not need ownership or broad privileges on unrelated application tables.
DO $$
BEGIN
    IF to_regclass('public.alembic_version') IS NOT NULL THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.alembic_version TO landcheck_migrator';
    END IF;
END
$$;

-- Tenant-protected Survey tables and queue metadata are owned by the migration role, never by the
-- runtime roles.  Missing optional legacy tables are skipped so this remains safe on an older
-- staging restore; the release preflight must still fail if a required runtime table is absent.
DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'plots',
        'hazard_analysis_jobs',
        'survey_georeference_sessions',
        'survey_users',
        'survey_auth_sessions',
        'survey_magic_link_tokens',
        'plot_export_jobs',
        'plot_meta',
        'plot_feature_overrides',
        'plot_subdivision_batches',
        'plot_subdivision_items'
    ] LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE public.%I OWNER TO landcheck_migrator', table_name);
        END IF;
    END LOOP;
END
$$;

-- The runtime roles must not own or bypass the policies.  FORCE ROW LEVEL SECURITY is deliberately
-- not enabled by this script; non-owner, non-BYPASSRLS roles are still subject to normal RLS.
ALTER ROLE landcheck_api NOBYPASSRLS;
ALTER ROLE landcheck_survey_worker NOBYPASSRLS;

