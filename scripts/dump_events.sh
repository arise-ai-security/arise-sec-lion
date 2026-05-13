#!/usr/bin/env bash
set -euo pipefail

# Dump the `events` table to a gzipped SQL file.
# Status messages go to stderr; the output path is the only thing on stdout
# so callers can do: OUT=$(./scripts/dump_events.sh)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/deployment/.env"

# Auto-source deployment/.env only if password not already supplied.
if [[ -z "${POSTGRES_PASSWORD:-}" && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-arise}"
POSTGRES_DB="${POSTGRES_DB:-arise_events}"

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "ERROR: POSTGRES_PASSWORD is required (set env var or populate $ENV_FILE)" >&2
  exit 1
fi

for bin in pg_dump psql gzip du date; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found in PATH" >&2
    exit 1
  fi
done

OUT_PATH="${1:-events-$(date -u +%Y%m%dT%H%M%SZ).sql.gz}"

echo "Dumping events from ${POSTGRES_USER}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB} -> $OUT_PATH" >&2

# --column-inserts: portable across PG major versions and resilient to schema
# tweaks (column order/additions) since each INSERT names its columns.
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
  --host="$POSTGRES_HOST" \
  --port="$POSTGRES_PORT" \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  --table=events \
  --data-only \
  --no-owner \
  --no-privileges \
  --column-inserts \
  | gzip -9 > "$OUT_PATH"

ROW_COUNT=$(PGPASSWORD="$POSTGRES_PASSWORD" psql \
  --host="$POSTGRES_HOST" \
  --port="$POSTGRES_PORT" \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  -tAc 'SELECT COUNT(*) FROM events;')

SIZE=$(du -h "$OUT_PATH" | cut -f1)

echo "  rows:  $ROW_COUNT" >&2
echo "  size:  $SIZE" >&2

# stdout: only the output path, so callers can pipe it.
echo "$OUT_PATH"
