#!/bin/sh
set -eu

# This script runs once as the application container command, before Uvicorn
# forks any workers. It is deliberately not imported by worker processes.
export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/var/lib/landcheck/prometheus}"
mkdir -p "$PROMETHEUS_MULTIPROC_DIR"

# The Compose lifecycle stops the previous API container before starting this
# fresh container. Only metric files are removed; the exporter and workers never
# perform this cleanup during their own startup.
find "$PROMETHEUS_MULTIPROC_DIR" -mindepth 1 -maxdepth 1 -type f -delete

alembic upgrade head
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${UVICORN_WORKERS:-2}" \
  --timeout-keep-alive "${UVICORN_KEEPALIVE:-30}"
