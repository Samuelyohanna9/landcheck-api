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
        -- Unlike the branch above, this must not include NOLOGIN: an admin sets LOGIN/password on
        -- an existing role through the separate secret-handling procedure, and re-running this
        -- script (e.g. after adding a newly created app table to ownership) must not silently
        -- revoke that - it already did once, taking the migrate service down.
        ALTER ROLE landcheck_migrator NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
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

-- Every pre-existing application table (Survey, Estates, Green, Work - all of them) must be owned
-- by the migration role, because Alembic connects as landcheck_migrator and DDL requires table
-- ownership (or an explicit grant) on anything it creates, alters or references via a foreign key.
-- These tables were created by an earlier admin role before this least-privilege layout existed, so
-- a hardcoded per-app table list here goes stale the moment a migration touches a table this script
-- doesn't already know about (that is exactly what broke the Estate construction-tracking release:
-- estate_plots was never in the old Survey-only list). Reassign anything in the public schema not
-- already owned by one of the roles this script manages. PostGIS's own catalog tables
-- (spatial_ref_sys, geometry_columns, etc.) are extension members, not application tables, and are
-- deliberately skipped.
DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_roles r ON r.oid = c.relowner
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND r.rolname NOT IN (
              'landcheck_migrator', 'landcheck_api', 'landcheck_survey_worker',
              'landcheck_maintenance', 'landcheck_superadmin'
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e'
          )
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO landcheck_migrator', rec.table_name);
    END LOOP;
END
$$;

-- The runtime roles must not own or bypass the policies.  FORCE ROW LEVEL SECURITY is deliberately
-- not enabled by this script; non-owner, non-BYPASSRLS roles are still subject to normal RLS.
ALTER ROLE landcheck_api NOBYPASSRLS;
ALTER ROLE landcheck_survey_worker NOBYPASSRLS;

