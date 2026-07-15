#!/usr/bin/env bash
set -euo pipefail

# Restore a gzipped (or plain) SQL dump of the `events` table.
# Safety: aborts if events table is non-empty unless --truncate or --append given.

MODE="abort"
DUMP_PATH=""
for arg in "$@"; do
  case "$arg" in
    --truncate) MODE="truncate" ;;
    --append)   MODE="append" ;;
    -h|--help)
      echo "Usage: $0 [--truncate|--append] <dump.sql[.gz]>" >&2
      exit 0 ;;
    *) DUMP_PATH="$arg" ;;
  esac
done

if [[ -z "$DUMP_PATH" ]]; then
  echo "Usage: $0 [--truncate|--append] <dump.sql[.gz]>" >&2
  exit 1
fi
if [[ ! -f "$DUMP_PATH" ]]; then
  echo "ERROR: dump file not found: $DUMP_PATH" >&2
  exit 1
fi

POSTGRES_USER="${POSTGRES_USER:-arise}"
POSTGRES_DB="${POSTGRES_DB:-arise_events}"
POSTGRES_CONTAINER="postgres-main"

for bin in docker gunzip; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' not found in PATH" >&2
    exit 1
  fi
done

psql_run() {
  docker exec -i "$POSTGRES_CONTAINER" psql \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" "$@"
}

EXISTING=$(psql_run -tAc 'SELECT COUNT(*) FROM events;' | tr -d '[:space:]')
if [[ "$EXISTING" -gt 0 && "$MODE" == "abort" ]]; then
  echo "ERROR: events table already has $EXISTING rows." >&2
  echo "       Re-run with --truncate (wipe first) or --append (let UNIQUE constraint deduplicate)." >&2
  exit 1
fi

echo "Restoring $DUMP_PATH -> ${POSTGRES_CONTAINER}/${POSTGRES_DB} (mode=$MODE, existing rows=$EXISTING)" >&2

# Stream the dump (with optional TRUNCATE prepended) into psql inside one
# transaction so a mid-restore failure can't leave the table empty/partial.
# ON_ERROR_STOP=1 ensures unique-violation collisions propagate via set -e.
{
  if [[ "$MODE" == "truncate" ]]; then echo "TRUNCATE events;"; fi
  if [[ "$DUMP_PATH" == *.gz ]]; then gunzip -c "$DUMP_PATH"; else cat "$DUMP_PATH"; fi
} | psql_run -v ON_ERROR_STOP=1 --single-transaction --quiet

NEW_COUNT=$(psql_run -tAc 'SELECT COUNT(*) FROM events;' | tr -d '[:space:]')
echo "  rows after restore: $NEW_COUNT" >&2
echo "$NEW_COUNT"
