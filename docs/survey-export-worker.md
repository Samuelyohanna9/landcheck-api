# Survey export worker operations

This document describes the first dedicated worker for Survey exports. It is intentionally
conservative: one worker process and one export at a time. It does not change the PostgreSQL
volume, the Cloudflare Tunnel or the existing synchronous export endpoints.

## Configuration

The API and worker must share the same export-cache volume and the same path:

```text
SURVEY_EXPORT_SHARED_DIR=/app/app/reports/export_jobs
```

The initial production values are:

```text
SURVEY_EXPORT_WORKER_CONCURRENCY=1
SURVEY_EXPORT_WORKER_POLL_SECONDS=2
SURVEY_EXPORT_WORKER_LEASE_SECONDS=900
SURVEY_EXPORT_WORKER_HEARTBEAT_SECONDS=30
SURVEY_EXPORT_WORKER_MAX_ATTEMPTS=3
SURVEY_EXPORT_DB_POOL_SIZE=2
SURVEY_EXPORT_DB_MAX_OVERFLOW=0
SURVEY_EXPORT_DB_POOL_TIMEOUT=15
```

Do not increase concurrency or memory limits without measurements from the isolated environment.

## Pre-deployment checks

Run from the backend checkout on Hetzner:

```sh
cd /root/landcheck-api
set -eu

docker compose config --quiet
docker compose ps
docker volume ls --format '{{.Name}}' | grep -E '(^|_)pgdata$|survey_export_files' || true
docker inspect "$(docker compose ps -q db)" --format '{{json .Mounts}}'
docker inspect "$(docker compose ps -q api)" --format '{{json .Mounts}}'
```

Stop if the API or database container is not healthy, if the PostgreSQL volume is not the expected
volume, or if the checkout contains unapproved production edits.

Create a release backup directory without exposing `.env` contents:

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/root/landcheck-deploy-backups/survey-worker-$STAMP"
install -d -m 700 "$BACKUP_DIR"

# Keep the source Compose file and non-secret topology information. Do not redirect
# `docker compose config` to a backup file: it can expand values from `.env`.
cp --preserve=all docker-compose.yml "$BACKUP_DIR/docker-compose.before.yml"
chmod 600 "$BACKUP_DIR/docker-compose.before.yml"
docker compose config --quiet
docker compose images > "$BACKUP_DIR/images.txt"
docker compose config --services > "$BACKUP_DIR/services.txt"
docker compose config --volumes > "$BACKUP_DIR/volumes.txt"
docker inspect "$(docker compose ps -q api)" \
  --format 'container={{.Name}} image={{.Config.Image}} id={{.Id}} mounts={{json .Mounts}}' \
  > "$BACKUP_DIR/api.inspect.txt"
docker inspect "$(docker compose ps -q db)" \
  --format 'container={{.Name}} image={{.Config.Image}} id={{.Id}} mounts={{json .Mounts}}' \
  > "$BACKUP_DIR/db.inspect.txt"
docker image inspect "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q api)")" \
  --format 'id={{.Id}} repo={{json .RepoTags}} digests={{json .RepoDigests}}' \
  > "$BACKUP_DIR/api-image.inspect.txt"

# Save the migration files as a rollback compatibility overlay. The previous image
# may know revision 0072 but not 0073/0074/0075, while the database will be stamped at 0075.
cp --preserve=all \
  alembic/versions/20261031_0073_survey_export_worker.py \
  "$BACKUP_DIR/20261031_0073_survey_export_worker.py"
chmod 600 "$BACKUP_DIR/20261031_0073_survey_export_worker.py"
cp --preserve=all \
  alembic/versions/20261031_0074_survey_export_jobs_table.py \
  "$BACKUP_DIR/20261031_0074_survey_export_jobs_table.py"
chmod 600 "$BACKUP_DIR/20261031_0074_survey_export_jobs_table.py"
cp --preserve=all \
  alembic/versions/20261101_0075_survey_rls_policies.py \
  "$BACKUP_DIR/20261101_0075_survey_rls_policies.py"
chmod 600 "$BACKUP_DIR/20261101_0075_survey_rls_policies.py"
```

Back up PostgreSQL using the existing credentials without printing them:

```sh
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/postgres.dump"

test -s "$BACKUP_DIR/postgres.dump"
docker run --rm -v "$BACKUP_DIR:/backup:ro" postgres:15 \
  pg_restore --list /backup/postgres.dump > "$BACKUP_DIR/postgres.restore-list.txt"
test -s "$BACKUP_DIR/postgres.restore-list.txt"
```

Do not continue if `pg_restore --list` fails. Do not run `docker compose down -v`, `docker system
prune`, volume pruning or database reset commands.

## Preserve existing export files

The new volume is mounted only at `app/reports/export_jobs`; it does not hide the other report,
preview, orthophoto or tile-cache directories. Before the first API recreation, preserve existing
completed export files from the current API container:

```sh
OLD_API=$(docker compose ps -q api)
mkdir -p "$BACKUP_DIR/export_jobs"
docker cp "$OLD_API:/app/app/reports/export_jobs/." "$BACKUP_DIR/export_jobs/" 2>/dev/null || true

docker volume create landcheck-api_survey_export_files >/dev/null
PREVIOUS_API_IMAGE=$(docker inspect "$OLD_API" --format '{{.Config.Image}}')
printf '%s\n' "$PREVIOUS_API_IMAGE" > "$BACKUP_DIR/previous-api-image.txt"
docker run --rm \
  -v "$BACKUP_DIR/export_jobs:/source:ro" \
  -v landcheck-api_survey_export_files:/destination \
  "$PREVIOUS_API_IMAGE" sh -c 'cp -a /source/. /destination/'
```

Verify the volume before replacing the API:

```sh
docker run --rm -v landcheck-api_survey_export_files:/data "$PREVIOUS_API_IMAGE" \
  sh -c 'find /data -type f -maxdepth 3 | head -20'
```

If the volume name differs from `landcheck-api_survey_export_files`, stop and use the exact name
reported by `docker volume ls`. Never guess a volume name.

## Migration and image preparation

The release contains additive migrations `20261031_0073`, `20261031_0074` and `20261101_0075`.
Revisions 0073/0074 add or preserve owner scope, lease, heartbeat, retry and progress fields,
indexes and the queue table while preserving existing jobs and files. Revision 0075 installs the
three Survey owner policies and enables RLS; it deliberately does not enable FORCE ROW LEVEL
SECURITY. No revision deletes existing jobs or files.

Build the release image once and retain the previous image reference:

```sh
docker compose build api
RELEASE_API_IMAGE="$(docker compose images -q api | head -1)"
test -n "$RELEASE_API_IMAGE"
docker image inspect "$RELEASE_API_IMAGE" >/dev/null
```

The Compose `migrate` service runs `alembic upgrade head` and
`scripts/verify_runtime_schema` once, using `LANDCHECK_MIGRATOR_DATABASE_URL`, before API workers
start. It deliberately does not run the legacy runtime schema bootstrap: existing legacy tables
must already be present, and their versioned changes must be represented by a reviewed migration
or a separately approved administrator operation. This is required because the migrator is not
the owner of every historical Green/Work table and must not attempt ad-hoc `ALTER TABLE` work.
The API entrypoint only starts Uvicorn, and the worker waits for the API healthcheck; neither
runtime service runs migrations or schema initialization.

The Release B role layout is applied separately by a reviewed administrator procedure. The
version-controlled `scripts/provision_least_privilege_roles.sql` contains no passwords and is not
run by an API or worker container. `landcheck_migrator` owns schema objects, while
`landcheck_api` and `landcheck_survey_worker` are NOSUPERUSER/NOBYPASSRLS and must not own tenant
tables. Set their LOGIN/password values through the approved secret-handling procedure, then set
`LANDCHECK_MIGRATOR_DATABASE_URL`, `LANDCHECK_API_DATABASE_URL` and
`SURVEY_EXPORT_DATABASE_URL` in the deployment environment. Keep
`LANDCHECK_RUNTIME_SCHEMA_BOOTSTRAP=false` for API and worker services.

Before any production role change, restore a current database backup into isolated PostGIS,
apply the role script, run the complete two-user/anonymous/RLS/export matrix and inventory all
non-Survey tables used by Estates, Green and Work. The initial role script uses a deliberately
conservative DML grant set; table-level tightening should follow that inventory, not guesswork.

## Controlled deployment

The API container will be recreated and may be unavailable briefly while the migration and health
check complete. PostgreSQL is not recreated or restarted.

```sh
# Start the one-shot migration service and wait for it to exit successfully. This is the only
# service allowed to run Alembic migrations. It must fail closed if a required legacy table is
# missing; do not substitute the legacy admin role or enable runtime bootstrap in production.
docker compose up migrate
MIGRATE_ID="$(docker compose ps -q migrate)"
test -n "$MIGRATE_ID"
test "$(docker inspect --format '{{.State.ExitCode}}' "$MIGRATE_ID")" = "0"
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version"' \
  | grep -Fx '20261101_0075'

# The API recreation may cause a brief interruption on port 8000. The DB and Cloudflare Tunnel
# are not recreated or restarted.
docker compose up -d --no-build --no-deps api
docker compose ps api
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null

docker compose up -d --no-build --no-deps metrics-exporter survey-export-worker
docker compose ps metrics-exporter survey-export-worker
```

Stop if the API healthcheck fails, if the worker is unhealthy, or if the database service changed.

Validate the worker and job state without exposing customer data:

```sh
docker compose logs --tail=100 survey-export-worker
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT version_num FROM alembic_version"'
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT status, count(*) FROM plot_export_jobs GROUP BY status"'
```

Submit one authorized Survey export and verify its existing status polling and download URL before
allowing normal traffic. Verify that two simultaneous requests for the same render options return
one live job and that a second user's job returns 404 from both status and download endpoints.

## Monitoring checks

Verify the existing private Prometheus exporter and Grafana dashboards. The worker does not expose
a public port.

```sh
docker compose ps metrics-exporter
docker compose logs --tail=50 metrics-exporter
docker exec "$(docker compose ps -q metrics-exporter)" \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9101/healthz').read().decode())"
```

Prometheus should show the worker metrics through the existing monitoring network. Confirm the
worker is reachable from the monitoring project using its configured exporter alias; do not publish
port 9101 to the host.

## Rollback

Rollback is additive and does not require deleting the migration or recreating PostgreSQL.

If the worker fails but the API is healthy:

```sh
docker compose stop survey-export-worker
docker compose ps api db
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null
```

Queued jobs remain in PostgreSQL. They are not deleted. The old API can continue serving legacy
synchronous exports and existing completed downloads.

If the new API image fails its healthcheck, restore the previous Compose file and immutable image
reference captured in the backup directory. Use a temporary override that sets only the API image;
do not use `--build`. Because the previous API startup may run Alembic and may not know Release B
revisions, mount the saved 0073, 0074 and 0075 migrations into the previous image so its Alembic
environment can recognize the additive database revision. Restore the previous application
environment, including its prior database URL, but do not delete the new roles yet. Do not stamp
the database backward and do not run `alembic downgrade`:

```sh
cat > "$BACKUP_DIR/rollback-api.override.yml" <<EOF
services:
  api:
    image: $PREVIOUS_API_IMAGE
    build: null
    volumes:
      - $BACKUP_DIR/20261031_0073_survey_export_worker.py:/app/alembic/versions/20261031_0073_survey_export_worker.py:ro
      - $BACKUP_DIR/20261031_0074_survey_export_jobs_table.py:/app/alembic/versions/20261031_0074_survey_export_jobs_table.py:ro
      - $BACKUP_DIR/20261101_0075_survey_rls_policies.py:/app/alembic/versions/20261101_0075_survey_rls_policies.py:ro
EOF

DB_BEFORE_ROLLBACK="$(docker compose ps -q db)"
docker compose \
  --project-directory /root/landcheck-api \
  --env-file /root/landcheck-api/.env \
  -f "$BACKUP_DIR/docker-compose.before.yml" \
  -f "$BACKUP_DIR/rollback-api.override.yml" \
  up -d --no-build --no-deps api

docker compose ps api db
test "$(docker compose ps -q db)" = "$DB_BEFORE_ROLLBACK"
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null
```

If the previous image cannot start with the compatibility overlay, stop and preserve the running
database and export volume while restoring the new image or following a separately reviewed
incident procedure. Do not use `git reset --hard`, `docker compose down -v`, Docker prune or any
command that recreates the `db` service.

The migrations are intentionally additive and the previous application can ignore the new
columns/table, but its old Alembic script directory cannot recognize revisions 0073, 0074 and
0075 by themselves. The compatibility overlay is therefore required for the automatic startup
migration. Keep the new roles until the rollback window closes; remove or revoke them only through
a separately reviewed role rollback after the old API is stable.
Do not run `alembic downgrade`
during an incident; dropping lease metadata is unnecessary and could remove recovery information.

## Known limitations

- Hard process termination during a native PDF/CAD renderer cannot be safely interrupted. Lease
  fencing prevents stale completion, and the next worker recovers the job after expiry.
- Files generated by the old container must be copied into the shared volume before the first API
  recreation, or old completed job rows may reference files that are no longer present.
- Real PostgreSQL/PostGIS worker execution, memory consumption and restart recovery must be tested
  in isolated staging before production.
