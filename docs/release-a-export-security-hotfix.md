# Release A — Survey export authorization hotfix

This release is standalone. It does not include the Survey worker, migrations 0073/0074,
database role changes, RLS refactoring, Docker Compose changes or SEO work.

## Release contents

- app/routers/plots.py
- tests/test_release_a_export_authorization.py
- docs/release-a-export-security-hotfix.md

The API reads the existing plot_export_jobs columns only. It derives job ownership from
plots.owner_user_id or the subdivision parent plot. No owner_user_id column is added to
plot_export_jobs, and no migration is required.

## Behavior

- Authenticated owners can submit, poll and download their own exports.
- Cross-user status and download requests return 404.
- Anonymous status, submission and download requests are denied.
- Ownerless legacy jobs are retained but return 404 until ownership is securely established.
- Existing anonymous plot creation and preview flows are unchanged.
- Existing synchronous final-file routes now require the authenticated owner.
- The deployed baseline has no cancellation endpoint; this release does not invent one.

## Staging validation

Use an isolated database whose plot_export_jobs table matches the deployed schema. Include
owned plots for users A and B, an ownerless plot, a subdivision batch, and completed jobs.
Verify:

1. User A can read and download an A-owned job.
2. User B receives 404 for that job's status and download.
3. No session receives access to an ownerless job.
4. User A cannot submit or download from a B-owned plot.
5. User A can submit and retrieve an export for an A-owned plot.
6. Subdivision export access follows the parent plot owner.
7. Existing authenticated frontend export URLs remain unchanged.

## Read-only production preflight

Run these checks on Hetzner before any release command. They do not change containers,
database data or configuration. Do not print the full Compose configuration because it may
contain environment values.

~~~sh
cd /root/landcheck-api
set -eu
docker compose ps
docker inspect "$(docker compose ps -q api)" --format 'API_IMAGE={{.Config.Image}} API_ID={{.Image}} API_CMD={{json .Config.Cmd}}'
docker inspect "$(docker compose ps -q db)" --format 'DB_IMAGE={{.Config.Image}} DB_MOUNTS={{range .Mounts}}{{.Destination}}={{.Name}} {{end}}'
df -h / /root/landcheck-api
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null

# Release A adds no migration. The existing start_api.sh runs `alembic upgrade head`
# before Uvicorn, so verify that production is already at the existing head and stop if
# any unrelated migration is pending. Do not run an upgrade manually for this release.
docker compose exec -T api alembic current
docker compose exec -T api alembic heads

# Metadata only: no customer rows and no credentials in output.
docker compose exec -T db sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off' <<'SQL'
SELECT table_name, column_name, data_type, udt_name
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND table_name IN ('plot_export_jobs', 'plot_subdivision_batches', 'plots')
 ORDER BY table_name, ordinal_position;
SQL
~~~

Stop if the API or database is unhealthy, if the current worktree is not the approved release,
if the API image cannot be identified, or if the available disk space is insufficient for the
backup and one additional API image.

## Deployment preparation

Run from the backend checkout. Stop after any failed check.

~~~sh
cd /root/landcheck-api
set -eu
RELEASE_SHA='<approved-release-commit-sha>'
test "$(git rev-parse "$RELEASE_SHA^{commit}")" = "$RELEASE_SHA"
test -z "$(git status --porcelain)"
git show --stat --oneline "$RELEASE_SHA"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/root/landcheck-deploy-backups/release-a-$STAMP"
install -d -m 700 "$BACKUP_DIR"
cp --preserve=all docker-compose.yml "$BACKUP_DIR/docker-compose.before.yml"
docker compose images > "$BACKUP_DIR/images.before.txt"
OLD_API=$(docker compose ps -q api)
OLD_DB=$(docker compose ps -q db)
test -n "$OLD_API" && test -n "$OLD_DB"
docker inspect "$OLD_API" > "$BACKUP_DIR/api.before.json"
docker inspect "$OLD_DB" > "$BACKUP_DIR/db.before.json"
PREVIOUS_API_IMAGE=$(docker inspect "$OLD_API" --format '{{.Config.Image}}')
printf '%s
' "$PREVIOUS_API_IMAGE" > "$BACKUP_DIR/previous-api-image.txt"

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'   > "$BACKUP_DIR/postgres.dump"
test -s "$BACKUP_DIR/postgres.dump"
docker run --rm -v "$BACKUP_DIR:/backup:ro" postgres:15   pg_restore --list /backup/postgres.dump > "$BACKUP_DIR/postgres.restore-list.txt"
test -s "$BACKUP_DIR/postgres.restore-list.txt"
~~~

## Build and deploy

Build the approved API image once. Release A contains no migration and no Survey worker. The
existing API startup script may execute a no-op `alembic upgrade head`; the preflight above is
required to confirm that no migration would be applied by this release.

~~~sh
docker compose build api
RELEASE_API_IMAGE=$(docker compose images -q api | head -1)
test -n "$RELEASE_API_IMAGE"
docker image inspect "$RELEASE_API_IMAGE" >/dev/null
printf '%s
' "$RELEASE_API_IMAGE" > "$BACKUP_DIR/release-api-image.txt"

# Brief interruption is expected while only the API container is recreated.
docker compose up -d --no-deps api
docker compose ps api db
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null
~~~

Do not run docker compose down -v, Docker prune, volume deletion, database reset, or any
command that recreates the db service. The Cloudflare Tunnel is not restarted.

## Smoke checks

Using two existing test accounts and a disposable owned plot:

- User A status for an A-owned job: expected 200 or the existing job state.
- User B status for the same job: expected 404.
- User B download for the same job: expected 404.
- No session status/download: expected 401/404 without metadata or filename.
- Ownerless legacy job: expected 404 for every ordinary user.
- User A submission for an A-owned plot: expected existing queued/completed response.
- User B submission for that plot: expected 404.
- Existing authenticated Survey frontend export, polling and download: must remain functional.

Do not print tokens, customer data, filenames or response bodies in logs.

## Rollback

Release A has no schema dependency, so rollback is an API-image rollback only:

~~~sh
cd /root/landcheck-api
set -eu
BACKUP_DIR='<release-a-backup-directory>'
PREVIOUS_API_IMAGE=$(cat "$BACKUP_DIR/previous-api-image.txt")
DB_BEFORE_ROLLBACK=$(docker compose ps -q db)
cat > "$BACKUP_DIR/rollback-api.override.yml" <<EOF
services:
  api:
    image: $PREVIOUS_API_IMAGE
EOF

# The override changes only the API image. It preserves the current Compose environment,
# command and mounts, and --no-deps prevents PostgreSQL from being recreated or restarted.
docker compose -f docker-compose.yml -f "$BACKUP_DIR/rollback-api.override.yml" \
  up -d --no-build --no-deps api
docker compose ps api db
test "$(docker compose ps -q db)" = "$DB_BEFORE_ROLLBACK"
curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null
~~~

If the release image is unhealthy, restore the captured Compose file and previous image reference
before recreating only api. Do not run Alembic downgrade, alter PostgreSQL, remove volumes or
restart unrelated services.
