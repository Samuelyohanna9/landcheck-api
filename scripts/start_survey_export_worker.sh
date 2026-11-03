#!/usr/bin/env sh
set -eu

: "${DATABASE_URL:?DATABASE_URL must be set for the Survey export worker}"

# Alembic migrations are owned by the API container's existing entrypoint. The worker waits for
# the API healthcheck in Compose and never changes the database schema itself.
exec python -m app.services.survey_export_worker
