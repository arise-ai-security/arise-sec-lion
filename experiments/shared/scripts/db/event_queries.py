"""Async SQL layer over the `events` table.

Two responsibilities, kept here because both need an `asyncpg.Connection`:
  - low-level fetchers that hit one or more SQL statements
  - convenience composers that combine a fetcher with the pure
    transforms in `transforms.py`

Pure transforms live in `transforms.py` so they can be tested without
asyncpg / a live Postgres.

Schema (from `infrastructure/sql/create_events_table.sql`):
    events(
        event_id        UUID PK,
        aggregate_id    UUID NOT NULL,
        sequence_number INT  NOT NULL,
        event_type      TEXT NOT NULL,
        payload         JSONB NOT NULL,
        occurred_at     TIMESTAMPTZ NOT NULL,
        metadata        JSONB DEFAULT '{}',
        UNIQUE(aggregate_id, sequence_number)
    )

Parent ↔ child edge: each spawn produces a `ChildSpawned` event
(emitted on the parent's `aggregate_id`, with `payload->>'child_id'`
pointing at the new child). The recursive walks below use that single
predicate in both directions (down = subtree, up = lineage) so the
edge set is symmetric.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import orjson

from experiments.shared.scripts.db.models import EventRow
from experiments.shared.scripts.db.transforms import (
    build_agent_trajectory,
    build_parent_trace,
    prettify_trajectory,
)


if TYPE_CHECKING:
    from uuid import UUID

    import asyncpg


# Optional runtime import — pure transforms can still be exercised on
# minimal environments without asyncpg installed.
try:
    import asyncpg as _asyncpg  # noqa: F401 - imported for side-effect only
    _ASYNCPG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ASYNCPG_AVAILABLE = False


# =============================================================================
# Connection helper — registers JSONB codec so payload comes back as dict
# =============================================================================


async def open_connection(
    *,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> asyncpg.Connection:
    """Open a connection and register a JSONB codec.

    Defaults read environment variables (`POSTGRES_HOST`, `POSTGRES_PORT`,
    `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`). The
    docker-compose hostname `db` is rewritten to `localhost` so the same
    `.env` file works for both in-container and host-side invocations.
    """
    if not _ASYNCPG_AVAILABLE:
        raise RuntimeError("asyncpg is not installed in this environment")

    import asyncpg  # local import; module-level import gated above

    resolved_host = host or os.environ.get("POSTGRES_HOST", "localhost")
    if resolved_host == "db":
        resolved_host = "localhost"

    conn = await asyncpg.connect(
        host=resolved_host,
        port=port or int(os.environ.get("POSTGRES_PORT", "5432")),
        user=user or os.environ.get("POSTGRES_USER", "arise"),
        password=password or os.environ.get("POSTGRES_PASSWORD", ""),
        database=database or os.environ.get("POSTGRES_DB", "arise_events"),
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: orjson.dumps(v).decode("utf-8"),
        decoder=orjson.loads,
        schema="pg_catalog",
    )
    return conn


# =============================================================================
# SQL queries
# =============================================================================
#
# Two recursive walks of the SAME edge — `ChildSpawned.child_id` — in
# opposite directions:
#
#   _SUBTREE_CTE  walks DOWN from the root, collecting every descendant
#   _LINEAGE_CTE  walks UP   from a node,  collecting every ancestor
#
# Matches the canonical walker at
# `infrastructure/adapters/postgres_event_store.py::PostgresEventStore`
# (`get_hierarchy_events_grouped`, lines 550-605). Using `ChildSpawned`
# rather than `AgentCreated.parent_id` matters because the two are
# persisted in separate transactions: a crash window can leave a
# `ChildSpawned` with no matching `AgentCreated`, and walking via
# `ChildSpawned` keeps us aligned with what the rest of the system sees.
#
# `UNION` (not `UNION ALL`) is the only cycle defense — both walks
# terminate on a cyclic spawn chain by deduplicating on agent_id.
# `(payload->>'child_id')::uuid` cast mirrors the canonical walker;
# if cast-safety hardening is ever added, fix both walkers together.

# Walks DOWN: root → all descendants.
# Returns every event whose aggregate is in the subtree.
_SUBTREE_CTE = """
WITH RECURSIVE descendants(agent_id) AS (
    SELECT $1::uuid
    UNION
    SELECT (e.payload->>'child_id')::uuid
    FROM events e
    JOIN descendants d ON e.aggregate_id = d.agent_id
    WHERE e.event_type = 'ChildSpawned'
)
SELECT event_id, aggregate_id, sequence_number, event_type,
       payload, occurred_at, metadata
FROM events
WHERE aggregate_id IN (SELECT agent_id FROM descendants)
ORDER BY occurred_at, sequence_number
"""

# Walks UP: node → ancestors (boss is the root because no ChildSpawned
# names the boss as its child_id, so recursion stops there).
# Returns every event whose aggregate is on the path root → node.
_LINEAGE_CTE = """
WITH RECURSIVE ancestors(agent_id) AS (
    SELECT $1::uuid
    UNION
    SELECT e.aggregate_id
    FROM events e
    JOIN ancestors a ON (e.payload->>'child_id')::uuid = a.agent_id
    WHERE e.event_type = 'ChildSpawned'
)
SELECT event_id, aggregate_id, sequence_number, event_type,
       payload, occurred_at, metadata
FROM events
WHERE aggregate_id IN (SELECT agent_id FROM ancestors)
ORDER BY occurred_at, sequence_number
"""

_AGENT_EVENTS_SQL = """
SELECT event_id, aggregate_id, sequence_number, event_type,
       payload, occurred_at, metadata
FROM events
WHERE aggregate_id = $1::uuid
ORDER BY sequence_number
"""

# `ChildCompleted` / `ChildFailed` are emitted on the *parent's*
# aggregate with `payload->>'child_id'` set to this child. They are the
# "reported to parent" tail and are not visible via a self-aggregate
# fetch. Text comparison avoids a uuid-cast failure on corrupt rows.
_PARENT_OUTCOME_SQL = """
SELECT event_id, aggregate_id, sequence_number, event_type,
       payload, occurred_at, metadata
FROM events
WHERE event_type IN ('ChildCompleted', 'ChildFailed')
  AND payload->>'child_id' = $1::text
ORDER BY occurred_at, sequence_number
"""


def _row_to_event(row: asyncpg.Record) -> EventRow:
    payload = row["payload"]
    metadata = row["metadata"] or {}
    # Defensive: decode if the JSONB codec wasn't registered upstream.
    if isinstance(payload, (bytes, bytearray, str)):
        payload = orjson.loads(payload)
    if isinstance(metadata, (bytes, bytearray, str)):
        metadata = orjson.loads(metadata)
    return EventRow(
        event_id=row["event_id"],
        aggregate_id=row["aggregate_id"],
        sequence_number=row["sequence_number"],
        event_type=row["event_type"],
        payload=payload,
        occurred_at=row["occurred_at"],
        metadata=metadata,
    )


# =============================================================================
# Low-level fetchers
# =============================================================================
#
# `fetch_run_events(boss)`:        BOSS ◀──── start
#                                  ├── Mgr-1 ◀
#                                  │   ├── Worker-A ◀
#                                  │   └── Worker-B ◀
#                                  └── Mgr-2 ◀
#                                  (all aggregates → all their events,
#                                   globally time-ordered)


async def fetch_run_events(
    conn: asyncpg.Connection, run_id: UUID
) -> list[EventRow]:
    """Every event in the subtree rooted at `run_id` (boss aggregate),
    ordered globally by `(occurred_at, sequence_number)`. Flat-mode
    runs (A-cells) have no children, so the result is just the boss's
    own events."""
    rows = await conn.fetch(_SUBTREE_CTE, run_id)
    return [_row_to_event(r) for r in rows]


# `fetch_lineage_events(node)`:    BOSS ◀
#                                  ├── Mgr-1 ◀
#                                  │   ├── Worker-A ◀──── target
#                                  │   └── Worker-B
#                                  └── Mgr-2
#                                  (only ancestors of target → all their
#                                   events, globally time-ordered)


async def fetch_lineage_events(
    conn: asyncpg.Connection, node_id: UUID
) -> list[EventRow]:
    """Every event from the ancestor chain `root → … → node_id`,
    globally time-ordered. When `node_id` is itself the boss, returns
    only the boss's own events (recursion's UP step finds nothing)."""
    rows = await conn.fetch(_LINEAGE_CTE, node_id)
    return [_row_to_event(r) for r in rows]


async def fetch_agent_events(
    conn: asyncpg.Connection, agent_id: UUID
) -> list[EventRow]:
    """Every event whose `aggregate_id == agent_id`, by sequence_number."""
    rows = await conn.fetch(_AGENT_EVENTS_SQL, agent_id)
    return [_row_to_event(r) for r in rows]


async def fetch_parent_outcome_events(
    conn: asyncpg.Connection, child_id: UUID
) -> list[EventRow]:
    """Parent's `ChildCompleted` / `ChildFailed` events referencing
    `child_id`. Empty for the root boss (it has no parent)."""
    rows = await conn.fetch(_PARENT_OUTCOME_SQL, str(child_id))
    return [_row_to_event(r) for r in rows]


# =============================================================================
# High-level composers (fetch + transform [+ prettify])
# =============================================================================
#
# `get_agent_trajectory(worker_a)`:  BOSS                       only Worker-A's
#                                    ├── Mgr-1 ┐               own events +
#                                    │   ├── Worker-A ◀  ────  Mgr-1's
#                                    │   └── Worker-B              ChildCompleted
#                                    └── Mgr-2                     tail


async def get_agent_trajectory(
    conn: asyncpg.Connection,
    agent_id: UUID,
    *,
    with_parent_outcome: bool = True,
    pretty: bool = False,
) -> list[EventRow] | list[str]:
    """Chain-of-behavior for one agent: own events ordered by
    `sequence_number`, optionally tailed with the parent's
    `ChildCompleted` / `ChildFailed` for this agent. `pretty=True`
    returns human-readable lines with `ThoughtCaptured` runs collapsed.
    """
    own = await fetch_agent_events(conn, agent_id)
    outcomes = (
        await fetch_parent_outcome_events(conn, agent_id)
        if with_parent_outcome else []
    )
    traj = build_agent_trajectory(own, outcomes)
    return prettify_trajectory(traj) if pretty else traj


# `get_parent_trace(worker_a)`:    BOSS ◀                ancestors' full
#                                  ├── Mgr-1 ◀          event streams,
#                                  │   ├── Worker-A ◀   globally time-
#                                  │   └── Worker-B         ordered
#                                  └── Mgr-2


async def get_parent_trace(
    conn: asyncpg.Connection,
    node_id: UUID,
    *,
    pretty: bool = False,
) -> list[EventRow] | list[str]:
    """Full event trace from boss down the ancestor chain to `node_id`,
    globally time-ordered. `pretty=True` collapses per-aggregate
    `ThoughtCaptured` runs."""
    events = await fetch_lineage_events(conn, node_id)
    trace = build_parent_trace(events)
    return prettify_trajectory(trace) if pretty else trace
