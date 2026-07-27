#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_PATH="$OUTPUT_DIR/landcheck_db_${TIMESTAMP}.sql.gz"

mkdir -p "$OUTPUT_DIR"
cd "$ROOT_DIR"

docker compose exec -T db sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > "$ARCHIVE_PATH"
sha256sum "$ARCHIVE_PATH" > "${ARCHIVE_PATH}.sha256"

echo "Backup created: $ARCHIVE_PATH"
echo "Checksum file: ${ARCHIVE_PATH}.sha256"
