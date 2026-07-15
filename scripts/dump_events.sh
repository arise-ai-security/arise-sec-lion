#!/usr/bin/env bash
set -euo pipefail

# Dump the `events` table to a gzipped SQL file.
# Status messages go to stderr; the output path is the only thing on stdout
# so callers can do: OUT=$(./scripts/dump_events.sh)

POSTGRES_USER="${POSTGRES_USER:-arise}"
POSTGRES_DB="${POSTGRES_DB:-arise_events}"
POSTGRES_CONTAINER="postgres-main"

for bin in docker gzip du date; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found in PATH" >&2
    exit 1
  fi
done

OUT_PATH="${1:-events-$(date -u +%Y%m%dT%H%M%SZ).sql.gz}"

echo "Dumping events from ${POSTGRES_CONTAINER}/${POSTGRES_DB} -> $OUT_PATH" >&2

# --column-inserts: portable across PG major versions and resilient to schema
# tweaks (column order/additions) since each INSERT names its columns.
docker exec "$POSTGRES_CONTAINER" pg_dump \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  --table=events \
  --data-only \
  --no-owner \
  --no-privileges \
  --column-inserts \
  | gzip -9 > "$OUT_PATH"

ROW_COUNT=$(docker exec "$POSTGRES_CONTAINER" psql \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" \
  -tAc 'SELECT COUNT(*) FROM events;')

SIZE=$(du -h "$OUT_PATH" | cut -f1)

echo "  rows:  $ROW_COUNT" >&2
echo "  size:  $SIZE" >&2

# stdout: only the output path, so callers can pipe it.
echo "$OUT_PATH"
