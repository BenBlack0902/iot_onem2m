#!/bin/bash
# backups/backup_postgres.sh
# Simple nightly pg_dump of the onem2m database into ./backups
# - Sources .env if present for POSTGRES_USER/POSTGRES_DB
# - Keeps the last 7 backups
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/backups"
ENV_FILE="$ROOT_DIR/.env"

# Defaults (will be overridden by .env if available)
POSTGRES_USER="onem2m"
POSTGRES_DB="onem2m"

# Load .env if present and export variables
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
fi

mkdir -p "$BACKUP_DIR"

TS="$(date +%F-%H%M%S)"
OUT_FILE="$BACKUP_DIR/onem2m-db-$TS.sql.gz"

echo "$(date -Is) Starting pg_dump to $OUT_FILE" >&2

# Run pg_dump inside the running postgres container and gzip the output
# Use docker exec to run pg_dump as the postgres user defined in env.
docker exec -i onem2m_postgres pg_dump -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip -c > "$OUT_FILE"

if [ $? -eq 0 ]; then
  echo "$(date -Is) pg_dump completed: $OUT_FILE" >&2
else
  echo "$(date -Is) pg_dump FAILED" >&2
  exit 1
fi

# Keep last 7 backups
ls -1t "$BACKUP_DIR"/onem2m-db-*.sql.gz 2>/dev/null | sed -e '1,7d' | xargs -r rm -f --

echo "$(date -Is) Backup routine finished" >&2
