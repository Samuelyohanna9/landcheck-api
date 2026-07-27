#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${LANDCHECK_API_BASE_URL:-http://127.0.0.1:8000}"
ADMIN_BEARER_TOKEN="${LANDCHECK_ADMIN_BEARER_TOKEN:-}"

if [[ -z "$ADMIN_BEARER_TOKEN" ]]; then
  echo "Set LANDCHECK_ADMIN_BEARER_TOKEN before running this script."
  exit 1
fi

curl -fsS \
  -H "Authorization: Bearer ${ADMIN_BEARER_TOKEN}" \
  "${API_BASE_URL%/}/green/admin/security/posture"

