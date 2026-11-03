#!/bin/sh
set -eu

# This script runs once as the application container command, before Uvicorn
# forks any workers. It is deliberately not imported by worker processes.
export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/var/lib/landcheck/prometheus}"
mkdir -p "$PROMETHEUS_MULTIPROC_DIR"

# A shared multiprocess directory may also contain live worker metrics. Never clear it implicitly:
# an API-only restart must not delete files written by the still-running Survey worker or exporter.
# Operators may set this flag only during a coordinated cold start after every metrics writer has
# been stopped. Counters and histograms are otherwise preserved across API restarts.
if [ "${LANDCHECK_PROMETHEUS_CLEAN_ON_START:-false}" = "true" ]; then
  find "$PROMETHEUS_MULTIPROC_DIR" -mindepth 1 -maxdepth 1 -type f -delete
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${UVICORN_WORKERS:-2}" \
  --timeout-keep-alive "${UVICORN_KEEPALIVE:-30}"
