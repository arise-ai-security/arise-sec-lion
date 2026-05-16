#!/usr/bin/env bash
set -u

ROOTS=(
  "327e0b44-1e1f-4b66-a7c8-bbf295b353d7"
  "10e1abd9-fb0e-409e-96cf-6dc8822f856d"
  "1d020c75-001b-4c42-9aff-5ec3965cf94a"
)

set -a
source deployment/.env
set +a

has_active_processes() {
  ps -ax -o command |
    grep -F "main.py -c /Users/garfield/PycharmProjects/arise-sec-lion/experiments/c1-batch-autogen/configs/C1-qwen-noverifier.yaml" |
    grep -v grep >/dev/null
}

while has_active_processes; do
  printf '===== %s =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ps -ax -o pid,ppid,etime,pcpu,pmem,command |
    grep -E "main.py -c .*/experiments/c1-batch-autogen|run_matrix|uv run python" |
    grep -v grep || true

  PGHOST=localhost \
  PGPORT="${POSTGRES_PORT:-5432}" \
  PGUSER="${POSTGRES_USER:-arise}" \
  PGDATABASE="${POSTGRES_DB:-arise_events}" \
  PGPASSWORD="${POSTGRES_PASSWORD}" \
    psql -X -v ON_ERROR_STOP=1 -F $'\t' -At <<'SQL' || true
WITH roots(id) AS (
  VALUES
    ('327e0b44-1e1f-4b66-a7c8-bbf295b353d7'::uuid),
    ('10e1abd9-fb0e-409e-96cf-6dc8822f856d'::uuid),
    ('1d020c75-001b-4c42-9aff-5ec3965cf94a'::uuid)
)
SELECT left(e.aggregate_id::text, 8), e.event_type, count(*), max(e.occurred_at)
FROM events e
JOIN roots r ON e.aggregate_id = r.id
GROUP BY left(e.aggregate_id::text, 8), e.event_type
ORDER BY 1, 2;
SQL

  for root in "${ROOTS[@]}"; do
    docker ps -a \
      --filter "label=arise.root_id=${root}" \
      --format "{{.ID}} {{.Status}} {{.Image}} {{.Names}} {{.Labels}}"
  done

  sleep 60
done

printf '===== %s monitor stopped: no matching main.py processes =====\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
