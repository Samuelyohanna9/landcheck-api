#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backup.sql.gz> [--force]"
  exit 1
fi

BACKUP_PATH="$1"
FORCE_FLAG="${2:-}"

if [[ ! -f "$BACKUP_PATH" ]]; then
  echo "Backup file not found: $BACKUP_PATH"
  exit 1
fi

if [[ "$FORCE_FLAG" != "--force" ]]; then
  echo "Refusing to restore without explicit --force flag."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

gzip -dc "$BACKUP_PATH" | docker compose exec -T db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

echo "Restore completed from: $BACKUP_PATH"
