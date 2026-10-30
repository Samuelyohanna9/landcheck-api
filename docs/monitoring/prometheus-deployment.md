# LandCheck Prometheus deployment runbook

This runbook deploys the approved release from a clean detached Git worktree.
It does not overwrite the dirty production checkout, modify Cloudflare Tunnel,
recreate PostgreSQL, or require a Git revert for rollback.

The release contains no Alembic migrations. Uvicorn has no supported parent
child-exit hook; normal worker shutdown cleans the live gauge, while SIGKILL or
hard crashes can leave stale in-progress values until the next API-container
startup.

The deployment has one Compose API service running two Uvicorn workers.
Recreating that service causes a brief API interruption while the old container
stops and the new healthcheck passes. PostgreSQL, its named volume, the
Cloudflare Tunnel, Grafana, Node Exporter and the Prometheus TSDB are not
restarted by the API deployment.

## 0. Prepare the release commit locally (do not run on Hetzner)

Run this section from the approved local backend checkout only after reviewing
the diff. It is an allowlist: the unrelated
`app/services/estates/marketing_render.py` change must remain unstaged.

~~~powershell
Set-Location C:\Users\User\Desktop\project\landcheck-api

git status --short
git diff --check

$ReleaseFiles = @(
  '.env.example',
  'app/main.py',
  'app/metrics.py',
  'app/metrics_exporter.py',
  'docker-compose.yml',
  'docs/monitoring/grafana-promql.md',
  'docs/monitoring/prometheus-deployment.md',
  'docs/monitoring/prometheus-landcheck.yml',
  'requirements.txt',
  'scripts/start_api.sh',
  'tests/test_prometheus_instrumentation.py'
)

git add -- $ReleaseFiles
git diff --cached --check
git diff --cached --name-only

if (git diff --cached --name-only -- app/services/estates/marketing_render.py) {
  throw 'STOP: unrelated marketing_render.py was staged.'
}

$Expected = $ReleaseFiles | Sort-Object
$Actual = git diff --cached --name-only | Sort-Object
if ((Compare-Object $Expected $Actual)) {
  throw 'STOP: staged files differ from the approved monitoring allowlist.'
}

git commit -m "Add Prometheus monitoring instrumentation"
git push origin main

$RELEASE_SHA = (git rev-parse HEAD).Trim()
$REMOTE_MAIN_SHA = (git ls-remote origin refs/heads/main).Split("`t")[0].Trim()
if ($REMOTE_MAIN_SHA -ne $RELEASE_SHA) {
  throw "STOP: origin/main is $REMOTE_MAIN_SHA, expected $RELEASE_SHA."
}
Write-Output "Approved RELEASE_SHA=$RELEASE_SHA"
~~~

Do not stage with `git add .` or `git add -A`. The printed `RELEASE_SHA` is the
only value to use on Hetzner. If the deployment workflow creates a release
commit through another approved path, obtain that exact commit hash from the
workflow and repeat the `git ls-remote` equality check before deployment.

## 1. Prepare and verify the approved release

Set RELEASE_SHA to the approved commit available on origin/main. Run each stage
separately and stop if a command fails.

~~~sh
set -eu
APP_DIR=/root/landcheck-api
cd "$APP_DIR"

RELEASE_SHA="${RELEASE_SHA:?Set RELEASE_SHA to the approved monitoring commit}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/root/landcheck-deploy-backups/$STAMP"
RELEASE_DIR="/root/landcheck-release-$STAMP"

install -d -m 700 "$BACKUP_DIR"

git fetch origin main
REMOTE_MAIN_SHA=$(git rev-parse origin/main)
test "$REMOTE_MAIN_SHA" = "$RELEASE_SHA"
git cat-file -e "$RELEASE_SHA^{commit}"
git show --no-patch --format='Release %H%n%ci%n%s' "$RELEASE_SHA"

CURRENT_HEAD=$(git rev-parse HEAD)
git diff --name-only "$CURRENT_HEAD" "$RELEASE_SHA" -- alembic \
  > "$BACKUP_DIR/alembic-changes"

if [ -s "$BACKUP_DIR/alembic-changes" ]; then
  echo "STOP: the release changes Alembic files." >&2
  cat "$BACKUP_DIR/alembic-changes" >&2
  exit 1
fi

if [ -e "$RELEASE_DIR" ]; then
  echo "STOP: release directory already exists." >&2
  exit 1
fi
~~~

The equality check prevents deploying a different or partially fetched commit.
Do not continue if `origin/main` is not exactly `RELEASE_SHA`.

## 2. Preserve production state and reconcile requirements

~~~sh
cp --preserve=all .env "$BACKUP_DIR/.env"
cp --preserve=all docker-compose.yml "$BACKUP_DIR/docker-compose.previous.yml"
cp --preserve=all requirements.txt "$BACKUP_DIR/requirements.txt.current"

if [ -e requirements.txt.before-monitoring ]; then
  cp --preserve=all requirements.txt.before-monitoring \
    "$BACKUP_DIR/requirements.txt.before-monitoring"
fi

git status --short
git diff HEAD -- requirements.txt > "$BACKUP_DIR/requirements.txt.local.diff"
git show "$RELEASE_SHA:requirements.txt" \
  > "$BACKUP_DIR/requirements.txt.approved"

CURRENT_PROM_COUNT=$(grep -Ec '^prometheus-client>=0.21,<1.0[[:space:]]*$' requirements.txt || true)
APPROVED_PROM_COUNT=$(grep -Ec '^prometheus-client>=0.21,<1.0[[:space:]]*$' "$BACKUP_DIR/requirements.txt.approved" || true)

if [ "$CURRENT_PROM_COUNT" -ne 1 ] || [ "$APPROVED_PROM_COUNT" -ne 1 ]; then
  echo "STOP: expected Prometheus dependency is not present exactly once." >&2
  exit 1
fi

sed '/^prometheus-client>=0.21,<1.0[[:space:]]*$/d' requirements.txt \
  > "$BACKUP_DIR/requirements.txt.current.without-monitoring"
sed '/^prometheus-client>=0.21,<1.0[[:space:]]*$/d' \
  "$BACKUP_DIR/requirements.txt.approved" \
  > "$BACKUP_DIR/requirements.txt.approved.without-monitoring"

if ! cmp -s \
  "$BACKUP_DIR/requirements.txt.current.without-monitoring" \
  "$BACKUP_DIR/requirements.txt.approved.without-monitoring"; then
  echo "STOP: requirements.txt has changes beyond the approved addition." >&2
  diff -u "$BACKUP_DIR/requirements.txt.approved" requirements.txt || true
  exit 1
fi

if [ -e requirements.txt.before-monitoring ] && \
   grep -Eq '^prometheus-client>=0.21,<1.0[[:space:]]*$' requirements.txt.before-monitoring; then
  echo "STOP: requirements.txt.before-monitoring already contains the monitoring dependency." >&2
  exit 1
fi
~~~

The production checkout remains untouched. Its existing uncommitted
`prometheus-client` line is reconciled by comparing files with that exact line
removed from both sides. The clean release worktree supplies the dependency for
the image build; no server-side `git reset`, checkout, overwrite or cleanup is
used. The untracked `requirements.txt.before-monitoring` file is preserved and
checked as evidence of the pre-monitoring state.

## 3. Back up the database, image and Compose configuration

~~~sh
docker compose --env-file .env config --quiet
docker compose ps

docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/landcheck-postgres.dump"

test -s "$BACKUP_DIR/landcheck-postgres.dump"
chmod 600 "$BACKUP_DIR/landcheck-postgres.dump"

# Validate that the custom-format dump is readable and contains a TOC. Run the
# client from the existing PostGIS container so no host package is assumed.
docker compose exec -T db pg_restore --list - \
  < "$BACKUP_DIR/landcheck-postgres.dump" \
  > "$BACKUP_DIR/landcheck-postgres.contents"
test -s "$BACKUP_DIR/landcheck-postgres.contents"

API_CONTAINER_ID=$(docker compose ps -q api)
test -n "$API_CONTAINER_ID"
API_IMAGE_ID=$(docker inspect --format '{{.Image}}' "$API_CONTAINER_ID")

printf '%s\n' "$API_CONTAINER_ID" > "$BACKUP_DIR/api-container-id"
printf '%s\n' "$API_IMAGE_ID" > "$BACKUP_DIR/api-image-id"
docker image inspect "$API_IMAGE_ID" >/dev/null
docker image tag "$API_IMAGE_ID" "landcheck-api:rollback-$STAMP"
~~~

Stop if the dump is empty, `pg_restore --list` fails or produces an empty TOC,
the API container cannot be identified, or the current image cannot be tagged.

## 4. Create a clean release checkout

~~~sh
git worktree add --detach "$RELEASE_DIR" "$RELEASE_SHA"
test -f "$RELEASE_DIR/Dockerfile"
test -f "$RELEASE_DIR/.dockerignore"
test -f "$RELEASE_DIR/requirements.txt"
test -f "$RELEASE_DIR/docker-compose.yml"
grep -Ec '^prometheus-client>=0.21,<1.0[[:space:]]*$' \
  "$RELEASE_DIR/requirements.txt"

test -r "$APP_DIR/.env"

# Compose resolves build: . relative to the release compose file. The explicit
# --env-file keeps secrets in the existing production file and never in Git.
docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  config --quiet

docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  config --volumes | grep -Fx 'prometheus_multiproc'

docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  config | grep -F '/var/lib/landcheck/prometheus'
~~~

The release worktree is separate from `/root/landcheck-api`. The checks verify
the release build context, tracked Dockerfile, `.dockerignore`, production
`.env` readability, named metrics volume and API/exporter mount paths without
printing the resolved secret environment. There are no host bind mounts in the
release Compose file; `pgdata` and `prometheus_multiproc` are named volumes.
The old production checkout and its `.env` remain in place.

## 5. Verify the monitoring network

The production network must be landcheck-monitoring_monitoring. It remains
overrideable with LANDCHECK_MONITORING_NETWORK.

~~~sh
NETWORK_NAME="${LANDCHECK_MONITORING_NETWORK:-landcheck-monitoring_monitoring}"
docker network inspect "$NETWORK_NAME" >/dev/null

PROMETHEUS_CONTAINER="${PROMETHEUS_CONTAINER:?Set PROMETHEUS_CONTAINER}"
docker inspect --format '{{json .NetworkSettings.Networks}}' \
  "$PROMETHEUS_CONTAINER" | grep -F "$NETWORK_NAME"
~~~

Stop if the network does not exist or Prometheus is not attached. Do not create
a replacement network while the existing monitoring network is available.

## 6. Discover and validate the existing Prometheus configuration

Do not assume `/etc/prometheus/prometheus.yml`, a host path, a Compose project
directory or a validation command. Discover the actual arrangement first:

~~~sh
docker inspect "$PROMETHEUS_CONTAINER" \
  --format 'image={{.Config.Image}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}'

docker inspect "$PROMETHEUS_CONTAINER" \
  --format '{{range .Mounts}}{{println .Type "source=" .Source "destination=" .Destination "rw=" .RW}}{{end}}'

docker exec "$PROMETHEUS_CONTAINER" sh -c \
  'printf "container config candidates:\n";
   for p in /etc/prometheus/prometheus.yml /etc/prometheus/prometheus.yaml /prometheus/prometheus.yml; do
     if [ -f "$p" ]; then printf "%s\n" "$p"; fi;
   done;
   printf "promtool:\n";
   command -v promtool || true'
~~~

From the output, set the exact container path and matching host bind-mount
source. A named volume or an image-baked configuration is not a host file and
must not be replaced using a guessed path.

~~~sh
PROMETHEUS_CONFIG_CONTAINER="${PROMETHEUS_CONFIG_CONTAINER:?Set from the discovery output}"
PROMETHEUS_CONFIG_HOST="${PROMETHEUS_CONFIG_HOST:?Set to the matching writable bind-mount source}"
test -f "$PROMETHEUS_CONFIG_HOST"
docker exec "$PROMETHEUS_CONTAINER" test -f "$PROMETHEUS_CONFIG_CONTAINER"

cp --preserve=all "$PROMETHEUS_CONFIG_HOST" \
  "$BACKUP_DIR/prometheus.yml.previous"

if docker exec "$PROMETHEUS_CONTAINER" sh -c 'command -v promtool >/dev/null 2>&1'; then
  docker exec "$PROMETHEUS_CONTAINER" \
    promtool check config "$PROMETHEUS_CONFIG_CONTAINER"
else
  PROMETHEUS_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$PROMETHEUS_CONTAINER")
  docker run --rm --entrypoint promtool \
    -v "$PROMETHEUS_CONFIG_HOST:$PROMETHEUS_CONFIG_CONTAINER:ro" \
    "$PROMETHEUS_IMAGE" check config "$PROMETHEUS_CONFIG_CONTAINER"
fi
~~~

If no writable host bind mount exists, stop and obtain the existing monitoring
Compose project and its approved configuration-management procedure. Do not
copy over an image-baked or named-volume configuration by guesswork.

## 7. Install the scrape job without replacing monitoring configuration

The release fragment is copied into the existing monitoring project for review;
the existing Prometheus configuration is edited by adding only the LandCheck
job under its current `scrape_configs`. Preserve every existing job, rule,
alert, Grafana dashboard and TSDB volume.

~~~sh
MONITORING_DIR="${MONITORING_DIR:?Set MONITORING_DIR to the existing monitoring Compose directory}"
MONITORING_COMPOSE_FILE="${MONITORING_COMPOSE_FILE:?Set MONITORING_COMPOSE_FILE to the existing monitoring Compose file}"

test -d "$MONITORING_DIR"
test -f "$MONITORING_COMPOSE_FILE"
install -m 0644 \
  "$RELEASE_DIR/docs/monitoring/prometheus-landcheck.yml" \
  "$MONITORING_DIR/prometheus-landcheck.yml"

# After merging only the fragment's scrape job into the discovered config:
grep -Fq 'landcheck-metrics-exporter:9101' "$PROMETHEUS_CONFIG_HOST"

if docker exec "$PROMETHEUS_CONTAINER" sh -c 'command -v promtool >/dev/null 2>&1'; then
  docker exec "$PROMETHEUS_CONTAINER" \
    promtool check config "$PROMETHEUS_CONFIG_CONTAINER"
else
  PROMETHEUS_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$PROMETHEUS_CONTAINER")
  docker run --rm --entrypoint promtool \
    -v "$PROMETHEUS_CONFIG_HOST:$PROMETHEUS_CONFIG_CONTAINER:ro" \
    "$PROMETHEUS_IMAGE" check config "$PROMETHEUS_CONFIG_CONTAINER"
fi

docker compose \
  --project-directory "$MONITORING_DIR" \
  -f "$MONITORING_COMPOSE_FILE" \
  config --quiet
~~~

Stop if the existing YAML cannot be validated or if the monitoring Compose
configuration changes services, volumes or networks unexpectedly. Reload only
Prometheus after the API exporter is healthy; use the existing monitoring
Compose project only if its documented reload method requires it. Do not
restart Grafana, Node Exporter, PostgreSQL or the Cloudflare Tunnel.

## 8. Build the approved API image once

~~~sh
cat > "$BACKUP_DIR/release-image.override.yml" <<EOF
services:
  api:
    image: landcheck-api:release-$RELEASE_SHA
  metrics-exporter:
    image: landcheck-api:release-$RELEASE_SHA
EOF

docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  config --quiet

docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  build api

RELEASE_IMAGE_ID=$(docker image inspect \
  --format '{{.Id}}' "landcheck-api:release-$RELEASE_SHA")
printf '%s\n' "$RELEASE_IMAGE_ID" > "$BACKUP_DIR/release-image-id"
~~~

Only api is built. The exporter uses the same image and is not built a second
time. Stop if the release image cannot be inspected.

## 9. Deploy only the verified image

~~~sh
docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  up -d --no-build api metrics-exporter

docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  ps
~~~

Only `api` and `metrics-exporter` are targeted. PostgreSQL is not recreated,
rebuilt or restarted. Expect a brief API interruption while the single API
container is recreated; its 20-second stop grace period bounds shutdown, and
the API healthcheck must pass before proceeding. The Cloudflare Tunnel remains
untouched.

## 10. Health checks and Prometheus reload

~~~sh
curl --fail --silent --show-error http://127.0.0.1:8000/health/

docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  exec -T metrics-exporter \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9101/healthz', timeout=5).read().decode())"

# There must be no host mapping such as 9101:9101.
docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$APP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  ps metrics-exporter

curl --fail --silent --show-error https://api.landcheck.online/health/
curl --fail --silent --show-error --head https://landcheck.online/
~~~

Reload only Prometheus after the exporter is healthy. Grafana, Node Exporter,
PostgreSQL and the Cloudflare Tunnel are untouched:

~~~sh
docker kill -s HUP "$PROMETHEUS_CONTAINER"
~~~

Verify the Prometheus target:

~~~sh
docker exec "$PROMETHEUS_CONTAINER" sh -c \
  'if command -v wget >/dev/null 2>&1; then
     wget -qO- http://127.0.0.1:9090/api/v1/targets
   else
     curl -fsS http://127.0.0.1:9090/api/v1/targets
   fi' \
  | grep -F 'landcheck-metrics-exporter:9101' \
  | grep -F '"health":"up"'
~~~

Then verify the Grafana queries from grafana-promql.md. Stop and roll back if
API health, exporter health, Cloudflare routing or the Prometheus target fails.

## 11. Roll back without Git revert, push, dependency rebuild or PostgreSQL recreation

This remains valid if the new image was built successfully but the new API
failed its healthcheck. It uses the saved old image and saved old Compose file,
with --no-build and --no-deps.

~~~sh
set -eu
cd /root/landcheck-api

BACKUP_DIR="/root/landcheck-deploy-backups/<timestamp>"
STAMP="<timestamp>"
RELEASE_DIR="/root/landcheck-release-<timestamp>"

# Stop only the release exporter using the release Compose definition. Do not
# remove its named volume. The explicit files are required because the old
# production checkout does not define this new service.
docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$BACKUP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  stop metrics-exporter || true
docker compose \
  --project-directory "$RELEASE_DIR" \
  --env-file "$BACKUP_DIR/.env" \
  -p landcheck-api \
  -f "$RELEASE_DIR/docker-compose.yml" \
  -f "$BACKUP_DIR/release-image.override.yml" \
  rm -f metrics-exporter || true

cat > "$BACKUP_DIR/rollback.override.yml" <<EOF
services:
  api:
    image: landcheck-api:rollback-$STAMP
    build: null
EOF

docker compose \
  --project-directory /root/landcheck-api \
  --env-file "$BACKUP_DIR/.env" \
  -p landcheck-api \
  -f "$BACKUP_DIR/docker-compose.previous.yml" \
  -f "$BACKUP_DIR/rollback.override.yml" \
  up -d --no-build --no-deps api

docker compose \
  --project-directory /root/landcheck-api \
  --env-file "$BACKUP_DIR/.env" \
  -p landcheck-api \
  -f "$BACKUP_DIR/docker-compose.previous.yml" \
  ps api

curl --fail --silent --show-error http://127.0.0.1:8000/health/
~~~

If Prometheus configuration was changed during a failed release, restore only
the saved configuration and reload Prometheus:

~~~sh
cp --preserve=all "$BACKUP_DIR/prometheus.yml.previous" \
  "$PROMETHEUS_CONFIG_HOST"

if docker exec "$PROMETHEUS_CONTAINER" sh -c 'command -v promtool >/dev/null 2>&1'; then
  docker exec "$PROMETHEUS_CONTAINER" \
    promtool check config "$PROMETHEUS_CONFIG_CONTAINER"
else
  PROMETHEUS_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$PROMETHEUS_CONTAINER")
  docker run --rm --entrypoint promtool \
    -v "$PROMETHEUS_CONFIG_HOST:$PROMETHEUS_CONFIG_CONTAINER:ro" \
    "$PROMETHEUS_IMAGE" check config "$PROMETHEUS_CONFIG_CONTAINER"
fi

docker kill -s HUP "$PROMETHEUS_CONTAINER"
~~~

The rollback uses the original API command, environment file, dependency and
volume configuration from docker-compose.previous.yml. PostgreSQL is not
targeted because rollback uses --no-deps api.

Do not run git reset --hard, docker compose down -v, docker system prune,
Docker volume pruning, database volume deletion or changes to unrelated
services.
