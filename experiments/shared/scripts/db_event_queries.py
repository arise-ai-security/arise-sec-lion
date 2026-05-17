"""Reusable Postgres event queries for DB-first experiment analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class DbEvent:
    """One raw row from the authoritative ``events`` table."""

    aggregate_id: str
    sequence_number: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    def as_metric_event(self) -> dict[str, Any]:
        """Flatten payload into the dict shape consumed by metric aggregators."""
        row = dict(self.payload)
        row["event_type"] = self.event_type
        row["aggregate_id"] = self.aggregate_id
        row["sequence_number"] = self.sequence_number
        if self.occurred_at is not None:
            row["occurred_at"] = self.occurred_at.isoformat()
        return row


@dataclass(frozen=True)
class TerminalStatusRow:
    """Root-aggregate terminal status derived directly from DB events."""

    run_id: str
    has_run_started: bool
    has_run_completed: bool
    has_work_completed: bool
    has_work_failed: bool
    failure_reason: str
    run_status: str
    worker_cost_event_count: int
    event_count: int
    min_sequence: int | None
    max_sequence: int | None


class DbEventRepository:
    """Read-only query suite over the authoritative event store."""

    def __init__(self, connection_string: str) -> None:
        self._connection_string = connection_string
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        async def init_connection(conn: asyncpg.Connection) -> None:
            await conn.set_type_codec(
                "jsonb",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )
            await conn.set_type_codec(
                "json",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )

        self._pool = await asyncpg.create_pool(
            self._connection_string,
            init=init_connection,
            min_size=1,
            max_size=4,
            statement_cache_size=0,
        )

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def fetch_events_for_aggregates(
        self,
        aggregate_ids: list[str],
    ) -> dict[str, list[DbEvent]]:
        """Fetch ordered raw events for enrolled aggregate ids."""
        if not aggregate_ids:
            return {}
        rows = await self._fetch(
            """
            SELECT
                aggregate_id,
                sequence_number,
                event_type,
                payload,
                occurred_at,
                metadata
            FROM events
            WHERE aggregate_id = ANY($1::uuid[])
            ORDER BY aggregate_id, sequence_number ASC
            """,
            _uuid_list(aggregate_ids),
        )

        grouped: dict[str, list[DbEvent]] = {run_id: [] for run_id in aggregate_ids}
        for row in rows:
            event = _event_from_record(row)
            grouped.setdefault(event.aggregate_id, []).append(event)
        return grouped

    async def fetch_terminal_status(
        self,
        aggregate_ids: list[str],
    ) -> dict[str, TerminalStatusRow]:
        """Run the enrolled-run terminal-status validation query."""
        if not aggregate_ids:
            return {}
        rows = await self._fetch(
            """
            SELECT
                aggregate_id AS run_id,
                bool_or(event_type = 'RunStarted') AS has_run_started,
                bool_or(event_type = 'RunCompleted') AS has_run_completed,
                bool_or(event_type = 'WorkCompleted') AS has_work_completed,
                bool_or(event_type = 'WorkFailed') AS has_work_failed,
                COALESCE(
                    max(payload->>'reason') FILTER (WHERE event_type = 'WorkFailed'),
                    ''
                ) AS failure_reason,
                COALESCE(
                    max(payload->>'status') FILTER (WHERE event_type = 'RunCompleted'),
                    ''
                ) AS run_status,
                count(*) FILTER (WHERE event_type = 'WorkerCostRecorded')::int
                    AS worker_cost_event_count,
                count(*)::int AS event_count,
                min(sequence_number)::int AS min_sequence,
                max(sequence_number)::int AS max_sequence
            FROM events
            WHERE aggregate_id = ANY($1::uuid[])
            GROUP BY aggregate_id
            """,
            _uuid_list(aggregate_ids),
        )

        result = {
            run_id: TerminalStatusRow(
                run_id=run_id,
                has_run_started=False,
                has_run_completed=False,
                has_work_completed=False,
                has_work_failed=False,
                failure_reason="",
                run_status="",
                worker_cost_event_count=0,
                event_count=0,
                min_sequence=None,
                max_sequence=None,
            )
            for run_id in aggregate_ids
        }
        for row in rows:
            status = _terminal_status_from_record(row)
            result[status.run_id] = status
        return result

    async def fetch_event_inventory(
        self,
        aggregate_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Count event types for enrolled aggregates."""
        if not aggregate_ids:
            return []
        rows = await self._fetch(
            """
            SELECT
                aggregate_id AS run_id,
                event_type,
                count(*)::int AS n
            FROM events
            WHERE aggregate_id = ANY($1::uuid[])
            GROUP BY aggregate_id, event_type
            ORDER BY aggregate_id, event_type
            """,
            _uuid_list(aggregate_ids),
        )
        return [
            {
                "run_id": str(row["run_id"]),
                "event_type": str(row["event_type"]),
                "n": int(row["n"]),
            }
            for row in rows
        ]

    async def fetch_orphan_aggregate_summaries(
        self,
        enrolled_run_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Find DB aggregate ids absent from the enrollment lockfile."""
        rows = await self._fetch(
            """
            WITH enrolled AS (
                SELECT unnest($1::uuid[]) AS run_id
            )
            SELECT
                e.aggregate_id AS run_id,
                count(*)::int AS event_count,
                min(e.sequence_number)::int AS min_sequence,
                max(e.sequence_number)::int AS max_sequence,
                min(e.occurred_at) AS first_occurred_at,
                max(e.occurred_at) AS last_occurred_at,
                bool_or(e.event_type = 'RunStarted') AS has_run_started,
                COALESCE(
                    max(e.payload->>'role') FILTER (WHERE e.event_type = 'AgentCreated'),
                    ''
                ) AS role,
                COALESCE(
                    max(e.payload->>'parent_id') FILTER (WHERE e.event_type = 'AgentCreated'),
                    ''
                ) AS parent_id,
                COALESCE(
                    max(e.payload->>'task_description')
                        FILTER (WHERE e.event_type = 'TaskAssigned'),
                    ''
                ) AS task_description,
                COALESCE(
                    max(COALESCE(
                        e.payload #>> '{domain_metadata,instance_id}',
                        e.payload->>'instance_id'
                    )) FILTER (WHERE e.event_type = 'RunStarted'),
                    ''
                ) AS instance_id
            FROM events e
            LEFT JOIN enrolled ON enrolled.run_id = e.aggregate_id
            WHERE enrolled.run_id IS NULL
            GROUP BY e.aggregate_id
            ORDER BY first_occurred_at NULLS LAST, e.aggregate_id
            """,
            _uuid_list(enrolled_run_ids),
        )
        return [_orphan_from_record(row) for row in rows]

    async def _fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        if self._pool is None:
            raise RuntimeError("DbEventRepository.connect() must be called before querying")
        async with self._pool.acquire() as conn:
            records = await conn.fetch(query, *args)
        return list(records)


def _uuid_list(values: list[str]) -> list[UUID]:
    return [UUID(value) for value in values]


def _event_from_record(row: Any) -> DbEvent:
    payload = row["payload"]
    metadata = row["metadata"]
    return DbEvent(
        aggregate_id=str(row["aggregate_id"]),
        sequence_number=int(row["sequence_number"]),
        event_type=str(row["event_type"]),
        payload=dict(payload) if isinstance(payload, dict) else {},
        occurred_at=row["occurred_at"],
        metadata=dict(metadata) if isinstance(metadata, dict) else None,
    )


def _terminal_status_from_record(row: asyncpg.Record) -> TerminalStatusRow:
    return TerminalStatusRow(
        run_id=str(row["run_id"]),
        has_run_started=bool(row["has_run_started"]),
        has_run_completed=bool(row["has_run_completed"]),
        has_work_completed=bool(row["has_work_completed"]),
        has_work_failed=bool(row["has_work_failed"]),
        failure_reason=str(row["failure_reason"] or ""),
        run_status=str(row["run_status"] or ""),
        worker_cost_event_count=int(row["worker_cost_event_count"] or 0),
        event_count=int(row["event_count"] or 0),
        min_sequence=_optional_int(row["min_sequence"]),
        max_sequence=_optional_int(row["max_sequence"]),
    )


def _orphan_from_record(row: asyncpg.Record) -> dict[str, Any]:
    first = row["first_occurred_at"]
    last = row["last_occurred_at"]
    return {
        "source_authority": "db_events",
        "run_id": str(row["run_id"]),
        "event_count": int(row["event_count"] or 0),
        "min_sequence": _optional_int(row["min_sequence"]),
        "max_sequence": _optional_int(row["max_sequence"]),
        "first_occurred_at": first.isoformat() if first is not None else "",
        "last_occurred_at": last.isoformat() if last is not None else "",
        "has_run_started": int(bool(row["has_run_started"])),
        "role": str(row["role"] or ""),
        "parent_id": str(row["parent_id"] or ""),
        "task_description": str(row["task_description"] or ""),
        "instance_id": str(row["instance_id"] or ""),
    }


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected int-compatible DB value, got {type(value).__name__}")
