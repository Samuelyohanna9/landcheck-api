# LandCheck security remediation plan

This plan is for review and staging only. It does not authorize a production migration,
container restart, role change, commit or deployment.

## Confirmed production findings

The previous authorized read-only inspection established the following facts without exposing
credentials or customer data:

| Finding | Severity | Consequence |
| --- | --- | --- |
| The deployed API connects as sam_admin, which is SUPERUSER and has BYPASSRLS. | Critical | Database RLS is not an effective boundary for requests made by this role. |
| The deployed role owns plots, hazard_analysis_jobs, survey_georeference_sessions and plot_export_jobs. | Critical | Ownership plus BYPASSRLS can bypass the intended tenant policies. |
| Deployed plot_export_jobs had no owner_user_id column or RLS policy. | Critical | UUID-only status/download routes could disclose another user's export status or file. |
| Deployed job status/download handlers did not perform an owner check. | Critical | A guessed or leaked job id was sufficient to reach the handler. |
| The deployed API ran legacy init_db() from every Uvicorn worker. | High | Two workers could race while creating legacy tables/sequences. |
| The deployed Survey worker was not present. | High | Queued jobs had no dedicated crash-recovering executor. |

These findings are distinct from the local synthetic RLS tests. Local tests do not prove the
production role is least privilege.

## Local Release A correction: export authorization

The local source now applies authorization at every asynchronous Survey export boundary:

- plot PDF, orthophoto, topographic map, DXF, shapefile, back-computation and technical-report job submission routes;
- subdivision survey-plan and clean-copy job submission routes;
- job status, cancellation and download routes;
- the shared insert helper, as defense in depth.

The server resolves ownership from the persisted plot or subdivision parent, never from request
JSON. The request must have a valid Survey session. A mismatched owner returns the same 404-style
not-found response as a missing object. A legacy job with no resolvable owner is denied. This is
intentional fail-closed behavior; it does not silently assign a legacy export to the current user.

The existing anonymous Survey plot-creation workflow is unchanged. Anonymous users must sign in
before creating or downloading a private export. No private anonymous export is authorized.

### Release A files

Release A was intentionally limited to the deployed authorization hotfix and its review
artifacts. It did not include the worker or database migrations:

- app/routers/plots.py
- tests/test_release_a_export_authorization.py
- docs/release-a-export-security-hotfix.md

The additive worker/RLS migrations 0073, 0074 and 0075 belong to Release B and must not be mixed
into a Release A rollback or verification.

### Historical Release A staging and deployment sequence

This section records the earlier security-hotfix procedure only. It is not the Release B
deployment sequence and must not be used to apply the worker/RLS changes.

Run each block separately and stop if its validation fails. Substitute the approved release SHA;
never use a floating tag for the release image.

~~~sh
cd /root/landcheck-api
set -eu

# 1. Confirm the release and worktree before touching containers.
git fetch origin main
RELEASE_SHA='<approved-release-commit-sha>'
test "$(git rev-parse "$RELEASE_SHA^{commit}")" = "$RELEASE_SHA"
test -z "$(git status --porcelain)"
git show --stat --oneline "$RELEASE_SHA"
~~~

~~~sh
# 2. Capture a recoverable backup. Do not print .env or expanded Compose output.
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/root/landcheck-deploy-backups/security-release-$STAMP"
install -d -m 700 "$BACKUP_DIR"
cp --preserve=all docker-compose.yml "$BACKUP_DIR/docker-compose.before.yml"
docker compose images > "$BACKUP_DIR/images.before.txt"
OLD_API=$(docker compose ps -q api)
OLD_DB=$(docker compose ps -q db)
test -n "$OLD_API" && test -n "$OLD_DB"
docker inspect "$OLD_API" > "$BACKUP_DIR/api.inspect.before.json"
docker inspect "$OLD_DB" > "$BACKUP_DIR/db.inspect.before.json"
PREVIOUS_API_IMAGE=$(docker inspect "$OLD_API" --format '{{.Config.Image}}')
printf '%s
' "$PREVIOUS_API_IMAGE" > "$BACKUP_DIR/previous-api-image.txt"

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'   > "$BACKUP_DIR/postgres.dump"
test -s "$BACKUP_DIR/postgres.dump"
docker run --rm -v "$BACKUP_DIR:/backup:ro" postgres:15   pg_restore --list /backup/postgres.dump > "$BACKUP_DIR/postgres.restore-list.txt"
test -s "$BACKUP_DIR/postgres.restore-list.txt"
~~~

~~~sh
# 3. Apply 0073/0074 once, using the approved release image or a separate migration job.
# Do not run this from multiple API containers.
docker compose run --rm --no-deps api alembic upgrade head
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc   "SELECT version_num FROM alembic_version"' | grep -Fx '20261031_0074'
~~~

~~~sh
# 4. Build once, record the immutable image id, and recreate API only.
docker compose build api
RELEASE_API_IMAGE=$(docker compose images -q api | head -1)
test -n "$RELEASE_API_IMAGE"
docker image inspect "$RELEASE_API_IMAGE" >/dev/null
printf '%s
' "$RELEASE_API_IMAGE" > "$BACKUP_DIR/release-api-image.txt"

# API recreation causes a brief interruption on port 8000. PostgreSQL and the Cloudflare
# Tunnel are not recreated or restarted.
docker compose up -d --no-deps api
docker compose ps api
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null
~~~

~~~sh
# 5. Start the dedicated worker only after API health passes.
docker compose up -d --no-build --no-deps survey-export-worker
docker compose ps survey-export-worker
docker compose logs --tail=100 survey-export-worker
~~~

Before opening normal traffic, perform authenticated user-A/user-B status and download checks,
an anonymous check, one same-owner export, one missing-owner legacy-job check, and a duplicate
submission check. A cross-user response must be 404 and must not reveal file metadata.

### Legacy ownerless-job recovery

Do not infer ownership from a browser, request payload, filename or job id. For legacy rows where
0074 cannot backfill an owner from the source plot or subdivision parent:

1. Inventory the ownerless rows in staging and record only job ids, export types and source ids.
2. Resolve each source through an authorized administrative review using the migration/maintenance
   path, without exposing the file to an ordinary user.
3. For a verified source owner, apply a reviewed, audited owner assignment before allowing status
   or download access. Re-run the user-A/user-B denial checks afterward.
4. For an anonymous or unresolvable source, keep the row and file intact but deny access. The user
   must sign in and submit a new export from an owned plot; do not silently transfer the old row.
5. Keep the inventory and review decision with the release backup. No production data should be
   deleted as part of this recovery.

### Release A rollback

If the new API is unhealthy, do not downgrade the database and do not recreate db:

~~~sh
cd /root/landcheck-api
set -eu
BACKUP_DIR='<security-release-backup-directory>'
PREVIOUS_API_IMAGE=$(cat "$BACKUP_DIR/previous-api-image.txt")
DB_BEFORE_ROLLBACK=$(docker compose ps -q db)

docker compose stop survey-export-worker || true
docker compose up -d --no-build --no-deps api
docker compose ps api db
test "$(docker compose ps -q db)" = "$DB_BEFORE_ROLLBACK"
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null
~~~

The production Compose file should be restored from the captured copy if it changed. If the
previous API image cannot start after the additive schema is applied, stop and preserve the
database and export volume; use the separately reviewed compatibility overlay in
docs/survey-export-worker.md. Do not run alembic downgrade, docker compose down -v, Docker
prune or volume deletion. The rollback restores the API image only and does not recreate
PostgreSQL.

## Release B design: least-privilege roles and worker hardening

Release B must be staged and reviewed independently from the urgent endpoint fix. No production
privilege change is included in this local patch.

| Role | Login | RLS bypass | Ownership | Intended use |
| --- | --- | --- | --- | --- |
| landcheck_migrator | controlled, short-lived | no | schema/migration ownership only | Alembic migrations and reviewed DDL |
| landcheck_api | yes | no | no tenant-table ownership | FastAPI request DML and approved functions |
| landcheck_survey_worker | yes | no | no tenant-table ownership | Queue claim/update and RLS-scoped export reads |
| landcheck_maintenance | controlled or NOLOGIN + SECURITY DEFINER functions | no by default | no tenant-table ownership | Georef retention and approved aggregate jobs |
| landcheck_superadmin | controlled, not API | no by default | no tenant-table ownership | Explicit audited administrative operations |

The protected tables must be owned by a schema/migration role, not the API or worker. Required
grants, sequence privileges, function execution and FORCE ROW LEVEL SECURITY decisions must be
validated in staging first. FORCE ROW LEVEL SECURITY is not enabled by this remediation and must
not be enabled in production as an untested hotfix.

The API and worker will continue setting transaction/request context through the existing Survey
session lifecycle. The worker must set context from the persisted job owner before reading the
source plot, and must clear it on commit, rollback, connection return and error. Maintenance
jobs need an explicit controlled function or maintenance role; they must not depend on accidental
superuser/BYPASSRLS behavior.

### Release B validation and rollback requirements

Before any production role change:

1. Restore a current production backup into an isolated PostgreSQL/PostGIS instance.
2. Create the proposed roles and grants there, with no production credentials.
3. Run the two-user, anonymous, NULL-owner, connection-reuse, worker and maintenance test matrix.
4. Confirm API and worker startup with two Uvicorn workers and one worker process.
5. Confirm georef retention and super-admin analytics through explicit least-privilege paths.
6. Record current role attributes, grants, table owners and policies for rollback.
7. Apply role changes in a maintenance window with API stopped or drained, then verify health and
   cross-tenant denial before reopening traffic.

Rollback is a reviewed SQL change that restores the captured role attributes/grants, not a Git
revert. Keep the old role available until the new API and worker have passed production smoke
checks. Never drop the old role or revoke its access before the rollback window closes.

## Initialization and overlapping containers

The Compose file now has one `migrate` service. It runs
`python -m scripts.initialize_api_schema`, `alembic upgrade head` and
`python -m scripts.verify_runtime_schema` once, using the dedicated migrator connection, before
the API dependency is allowed to start. The API entrypoint only prepares Prometheus metrics and
executes Uvicorn; it no longer performs schema writes. The Uvicorn workers and Survey worker do
not run migrations or legacy schema bootstrap.

The deployment must not bypass the `migrate` service with `docker compose up --no-deps api`, and
must not start two release migration jobs against the same database. If a blue/green or parallel
deployment is later required, keep the one-shot migration/bootstrap step as a separately locked
release job before either API set starts.

Because the API and Survey worker share the Prometheus multiprocess directory, the API entrypoint
does not clear metric files by default. `LANDCHECK_PROMETHEUS_CLEAN_ON_START=true` is permitted
only during a coordinated cold start after every metrics writer has stopped; otherwise counters
and histograms survive API restarts and active worker files cannot be deleted accidentally.

## Current release gates

| Gate | Status | Reason |
| --- | --- | --- |
| Local export status/download owner checks | PASS locally | Focused tests cover owner match, mismatch, missing owner and authentication. |
| Local export submission authorization | PASS locally | All asynchronous plot and subdivision job routes call explicit checks. |
| Production export authorization | BLOCKED until Release A deploy | The deployed release was previously confirmed vulnerable. |
| Production role least privilege | BLOCKED | Requires the separately approved role migration and staging grant matrix. |
| Production worker deployment | BLOCKED | Worker is not deployed until Release A/B approval. |
| Production RLS effectiveness | BLOCKED under current role | sam_admin SUPERUSER/BYPASSRLS/owner facts must be remediated in Release B.

