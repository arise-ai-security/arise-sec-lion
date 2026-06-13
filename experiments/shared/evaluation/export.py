"""Export raw event rows for a set of runs as a re-importable PostgreSQL dump.

For each run, all events in its hierarchy (boss + descendants via ``ChildSpawned``)
are fetched as **raw rows** (exact stored bytes — no re-serialization) and rendered
as ``INSERT ... ON CONFLICT DO NOTHING`` statements that can be replayed with
``psql`` into an ``events`` table.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from config.settings import ApiSettings
from experiments.shared.evaluation.loading import REPO_ROOT


if TYPE_CHECKING:
    from collections.abc import Sequence


logger = logging.getLogger(__name__)

_COLUMNS = "event_id, aggregate_id, sequence_number, event_type, payload, occurred_at, metadata"
_SCHEMA_PATH = REPO_ROOT / "infrastructure" / "sql" / "create_events_table.sql"
_INSERT_BATCH = 500

# Raw rows for a run's whole hierarchy. ``payload``/``metadata`` are cast to text so
# the exact stored JSON is returned verbatim (bypassing the jsonb decode codec).
_HIERARCHY_ROWS_SQL = """
WITH RECURSIVE hierarchy AS (
    SELECT $1::uuid AS agent_id
    UNION
    SELECT (e.payload->>'child_id')::uuid AS agent_id
    FROM events e
    INNER JOIN hierarchy h ON e.aggregate_id = h.agent_id
    WHERE e.event_type = 'ChildSpawned'
)
SELECT e.event_id, e.aggregate_id, e.sequence_number, e.event_type,
       e.payload::text AS payload, e.occurred_at, e.metadata::text AS metadata
FROM events e
INNER JOIN hierarchy h ON e.aggregate_id = h.agent_id
ORDER BY e.aggregate_id, e.sequence_number
"""


def _sql_literal(value: object) -> str:
    """Render a value as a single-quoted SQL string literal (``NULL`` if None)."""
    if value is None:
        return "NULL"
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return "'" + text.replace("'", "''") + "'"


def _row_values(row: dict[str, Any]) -> str:
    return (
        f"({_sql_literal(row['event_id'])}, {_sql_literal(row['aggregate_id'])}, "
        f"{int(row['sequence_number'])}, {_sql_literal(row['event_type'])}, "
        f"{_sql_literal(row['payload'])}::jsonb, "
        f"{_sql_literal(row['occurred_at'])}::timestamptz, "
        f"{_sql_literal(row['metadata'])}::jsonb)"
    )


def render_events_dump(
    rows: Sequence[dict[str, Any]], *, include_schema: bool = True, table: str = "events"
) -> str:
    """Render event rows as a re-importable SQL dump.

    Args:
        rows: Mappings with keys event_id, aggregate_id, sequence_number, event_type,
            payload (JSON text), occurred_at (datetime), metadata (JSON text).
        include_schema: Prepend the ``events`` table DDL so the dump is self-contained.
        table: Target table name for the INSERTs.

    Returns:
        SQL text. ``standard_conforming_strings`` is forced on so backslashes in the
        JSON payloads are preserved literally.
    """
    parts = [
        f"-- arise-sec-lion events dump ({len(rows)} rows)",
        "SET standard_conforming_strings = on;",
        "",
    ]
    if include_schema and _SCHEMA_PATH.is_file():
        parts.extend((_SCHEMA_PATH.read_text(encoding="utf-8").strip(), ""))

    header = f"INSERT INTO {table} ({_COLUMNS}) VALUES"
    for start in range(0, len(rows), _INSERT_BATCH):
        chunk = rows[start : start + _INSERT_BATCH]
        values = ",\n  ".join(_row_values(row) for row in chunk)
        parts.append(
            f"{header}\n  {values}\nON CONFLICT (aggregate_id, sequence_number) DO NOTHING;\n"
        )
    return "\n".join(parts)


async def _fetch_hierarchy_rows(store: Any, root_id: UUID) -> list[dict[str, Any]]:
    if store.pool is None:
        msg = "Event store is not connected"
        raise RuntimeError(msg)
    async with store.pool.acquire() as conn:
        records = await conn.fetch(_HIERARCHY_ROWS_SQL, root_id)
    return [dict(record) for record in records]


async def dump_runs_sql(
    run_ids: Sequence[str | UUID],
    *,
    include_schema: bool = True,
    out_path: str | Path | None = None,
) -> str:
    """Produce a PostgreSQL dump of all events for the given runs' hierarchies.

    Args:
        run_ids: Run/root (BOSS) aggregate ids to export.
        include_schema: Prepend the events table DDL (self-contained dump).
        out_path: If given, also write the dump to this path.

    Returns:
        The SQL dump text. Events shared across runs are de-duplicated by event_id.
    """
    settings = ApiSettings.load()

    from infrastructure.adapters.postgres_event_store import PostgresEventStore

    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        seen: set[UUID] = set()
        rows: list[dict[str, Any]] = []
        for run_id in run_ids:
            root_id = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
            for row in await _fetch_hierarchy_rows(store, root_id):
                if row["event_id"] not in seen:
                    seen.add(row["event_id"])
                    rows.append(row)
    finally:
        await store.disconnect()

    dump = render_events_dump(rows, include_schema=include_schema)
    if out_path is not None:
        Path(out_path).write_text(dump, encoding="utf-8")
        logger.info("Wrote %d event rows to %s", len(rows), out_path)
    return dump
