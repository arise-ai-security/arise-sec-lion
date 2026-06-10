"""HARD-constraint recoverability test for the shared code-prefix cache.

The non-negotiable invariant (spec
``docs/superpowers/specs/2026-06-10-shared-code-prefix-cache-design.md`` ->
"HARD CONSTRAINT"): after a run, BOTH the captured file contents AND per-worker
cost must be fully recoverable from the event store DB alone.

Unlike ``test_shared_code_context.py`` (which round-trips live Python event
objects through an in-memory store), this test persists every event in its
**serialized DB form** -- the exact ``(event_type, payload_json)`` pair the
Postgres store writes (``event.__class__.__name__`` + ``model_dump(mode="json")``)
-- and rebuilds *only* from those bytes by re-deserializing through
``EVENT_TYPE_REGISTRY``. So the block and the costs are reconstructed from what
the DB holds, not from in-memory state, which is the actual recoverability claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from core.domain.aggregates.agent_session import AgentSession
from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    DomainEvent,
    SourceFileEdited,
    SourceFileObserved,
    WorkerCostRecorded,
)
from core.domain.values.subtask import Subtask
from infrastructure.adapters.postgres_event_store import EVENT_TYPE_REGISTRY
from infrastructure.adapters.worker.shared_code_context import SharedCodeContextProvider


@dataclass
class _SerializedRow:
    """One persisted event exactly as the DB stores it: type tag + JSON payload."""

    aggregate_id: UUID
    sequence_number: int
    event_type: str
    payload_json: str


@dataclass
class _SerializingEventStore:
    """In-memory store that persists events as serialized DB rows, not objects.

    Mirrors the Postgres store's round-trip: ``append`` serializes via
    ``__class__.__name__`` + ``model_dump(mode="json")`` (then JSON), and every
    read path reconstructs fresh objects through ``EVENT_TYPE_REGISTRY``. No live
    event object survives a write -> a read, so anything a reader sees was
    genuinely recovered from the DB representation.
    """

    rows: list[_SerializedRow] = field(default_factory=list)

    async def append(self, event: DomainEvent) -> None:
        if any(
            r.aggregate_id == event.aggregate_id
            and r.sequence_number == event.sequence_number
            for r in self.rows
        ):
            raise ValueError(
                f"OCC conflict: ({event.aggregate_id}, {event.sequence_number}) exists"
            )
        self.rows.append(
            _SerializedRow(
                aggregate_id=event.aggregate_id,
                sequence_number=event.sequence_number,
                event_type=event.__class__.__name__,
                payload_json=json.dumps(event.model_dump(mode="json")),
            )
        )

    async def append_batch(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.append(event)

    @staticmethod
    def _hydrate(row: _SerializedRow) -> DomainEvent:
        event_class = EVENT_TYPE_REGISTRY[row.event_type]
        return event_class(**json.loads(row.payload_json))

    async def get_events(
        self,
        aggregate_id: UUID,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> list[DomainEvent]:
        rows = sorted(
            (r for r in self.rows if r.aggregate_id == aggregate_id),
            key=lambda r: r.sequence_number,
        )
        if after_sequence is not None:
            rows = [r for r in rows if r.sequence_number > after_sequence]
        if limit is not None:
            rows = rows[:limit]
        return [self._hydrate(r) for r in rows]

    async def get_hierarchy_events_grouped(
        self, root_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        # Resolve reachability from serialized rows (ChildSpawned links), same as
        # the recursive CTE -- so even the hierarchy walk is DB-derived.
        reachable: set[UUID] = {root_id}
        frontier = [root_id]
        while frontier:
            current = frontier.pop()
            for row in self.rows:
                if row.aggregate_id != current or row.event_type != "ChildSpawned":
                    continue
                child = UUID(json.loads(row.payload_json)["child_id"])
                if child not in reachable:
                    reachable.add(child)
                    frontier.append(child)
        grouped: dict[UUID, list[DomainEvent]] = {}
        for agg in reachable:
            grouped[agg] = await self.get_events(agg)
        return grouped


async def _seed_worker(
    store: _SerializingEventStore, *, root_id: UUID, agent_id: UUID
) -> None:
    """Create a worker aggregate (AgentCreated) wired under the root via ChildSpawned."""
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.0, "max_tokens": 1000},
        "tool": "openhands",
    }
    await store.append(
        AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role="worker",
            parent_id=None if agent_id == root_id else root_id,
            config=config,
        )
    )
    if agent_id != root_id:
        root_rows = [r for r in store.rows if r.aggregate_id == root_id]
        next_seq = max((r.sequence_number for r in root_rows), default=0) + 1
        await store.append(
            ChildSpawned(
                aggregate_id=root_id,
                sequence_number=next_seq,
                child_id=agent_id,
                child_role="worker",
                subtask=Subtask(description="worker", config={}),
                child_config={},
            )
        )


async def _emit_worker_cost(
    store: _SerializingEventStore, *, agent_id: UUID, cost_usd: float
) -> None:
    """Emit ONE WorkerCostRecorded on a worker's own aggregate (the reverted path).

    The reverted design has each fresh-conversation worker record its own cost
    directly -- no shared session, no per-worker delta math. We reproduce that by
    constructing the event on the worker's aggregate and appending it, mirroring
    ``apply_worker_event`` re-sequencing onto the single in-memory aggregate.
    """
    existing = await store.get_events(agent_id)
    next_seq = max((e.sequence_number for e in existing), default=0) + 1
    worker = WorkerCostRecorded(
        aggregate_id=agent_id,
        sequence_number=next_seq,
        tool_name="openhands",
        model="gpt-5.4-mini",
        prompt_tokens=1000,
        completion_tokens=200,
        cache_read_tokens=800,
        cost_usd=cost_usd,
        duration_seconds=1.0,
    )
    await store.append(worker)


def _rebuild_entries_from_db(
    store: _SerializingEventStore, root_id: UUID
) -> list[dict[str, object]]:
    """Recover the canonical append-only entry log from serialized rows ALONE.

    Reuses the provider's own pure reducers, but feeds them events hydrated
    fresh from the JSON payloads, proving the block is derivable from DB bytes
    with no provider instance / memo / disk involved.
    """
    source_rows = [
        store._hydrate(r)
        for r in store.rows
        if r.event_type in ("SourceFileObserved", "SourceFileEdited")
    ]
    # Mirror the recursive-CTE hierarchy scope so we only count this run's rows.
    ordered = SharedCodeContextProvider._collect_source_events({root_id: source_rows})  # noqa: SLF001
    return SharedCodeContextProvider._build_entries(ordered)  # noqa: SLF001


@pytest.mark.asyncio
async def test_block_and_costs_fully_recoverable_from_event_store_alone() -> None:
    # Given: a synthetic multi-worker run. Two workers each view source and each
    # records its own cost; one file is edited then re-viewed by a later worker.
    store = _SerializingEventStore()
    root_id = uuid4()  # worker A is the root of this run
    worker_b = uuid4()
    worker_c = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=worker_b)
    await _seed_worker(store, root_id=root_id, agent_id=worker_c)

    provider = SharedCodeContextProvider(event_store=store)

    # Worker A views two files.
    await provider.record_view(root_id, root_id, "/src/a.c", "A-v1\n")
    await provider.record_view(root_id, root_id, "/src/util.h", "UTIL\n")
    # Worker B views a third file, then edits util.h.
    await provider.record_view(root_id, worker_b, "/src/b.c", "B-v1\n")
    await provider.record_edit(root_id, worker_b, "/src/util.h")
    # Worker C re-views util.h after the edit with new bytes.
    await provider.record_view(root_id, worker_c, "/src/util.h", "UTIL-v2-EDITED\n")

    # Each worker records its own cost (per-worker, no delta).
    costs = {root_id: 0.11, worker_b: 0.07, worker_c: 0.05}
    for agent_id, cost in costs.items():
        await _emit_worker_cost(store, agent_id=agent_id, cost_usd=cost)

    # The "live" block the run would have injected (provider, warm memo).
    live_block = await provider.code_block(root_id)
    assert live_block is not None

    # When: we throw away every provider/in-memory artifact and rebuild from the
    # serialized DB rows alone -- both the file block and the per-worker costs.
    cold = SharedCodeContextProvider(event_store=store)  # cold memo, no shared state
    recovered_block = await cold.code_block(root_id)

    db_entries = _rebuild_entries_from_db(store, root_id)
    grouped = await store.get_hierarchy_events_grouped(root_id)
    recovered_costs: dict[UUID, list[WorkerCostRecorded]] = {
        agg: [e for e in events if isinstance(e, WorkerCostRecorded)]
        for agg, events in grouped.items()
    }

    # Then (contents): the block is byte-identical when rebuilt purely from the DB.
    assert recovered_block == live_block

    # And the canonical APPEND-ONLY entry log is exactly what we observed: views
    # in arrival order, then the edit's stale note, then the post-edit revision
    # appended LAST — earlier entries (including pre-edit bytes) never rewritten
    # or reordered.
    assert [(e["kind"], e["path"]) for e in db_entries] == [
        ("file", "/src/a.c"),
        ("file", "/src/util.h"),
        ("file", "/src/b.c"),
        ("note", "/src/util.h"),
        ("file", "/src/util.h"),
    ]
    file_entries = [e for e in db_entries if e["kind"] == "file"]
    assert [e["content"] for e in file_entries] == [
        "A-v1\n",
        "UTIL\n",
        "B-v1\n",
        "UTIL-v2-EDITED\n",
    ]
    assert file_entries[-1]["revision"] == 2

    # And the live block rendered that log: stale copy retained, note after it,
    # the new revision last.
    rev1 = '<file path="/src/util.h" revision="1">'
    note = '<file_modified path="/src/util.h"/>'
    rev2 = '<file path="/src/util.h" revision="2">'
    assert live_block.index(rev1) < live_block.index(note) < live_block.index(rev2)
    assert "UTIL-v2-EDITED" in live_block

    # Then (cost): EACH worker has its OWN WorkerCostRecorded -- present, per-worker,
    # and exactly one (no double-count, no cross-worker delta inflation).
    for agent_id, cost in costs.items():
        recorded = recovered_costs.get(agent_id, [])
        assert len(recorded) == 1, f"worker {agent_id} must have exactly one cost event"
        assert recorded[0].aggregate_id == agent_id  # cost lives on the worker itself
        assert recorded[0].cost_usd == pytest.approx(cost)

    # The recovered total equals the sum of the per-worker inputs -- so the revert
    # of the conversation-reuse delta hack did NOT double-count anywhere.
    recovered_total = sum(
        e.cost_usd for events in recovered_costs.values() for e in events
    )
    assert recovered_total == pytest.approx(sum(costs.values()))

    # And every persisted SourceFileObserved carries a sha256 matching its content,
    # so the captured bytes are integrity-checkable straight from the DB.
    for row in store.rows:
        if row.event_type != "SourceFileObserved":
            continue
        event = store._hydrate(row)  # noqa: SLF001
        assert isinstance(event, SourceFileObserved)
        import hashlib

        assert event.content_sha256 == hashlib.sha256(event.content.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_recovered_block_independent_of_replay_order_of_rows() -> None:
    # Given: a run with several source events persisted to the DB.
    store = _SerializingEventStore()
    root_id = uuid4()
    worker_b = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=worker_b)
    provider = SharedCodeContextProvider(event_store=store)
    await provider.record_view(root_id, root_id, "/src/a.c", "A\n")
    await provider.record_view(root_id, worker_b, "/src/b.c", "B\n")
    expected = await provider.code_block(root_id)

    # When: the DB rows are physically shuffled (storage order is not guaranteed)
    # and a cold provider rebuilds -- ordering must come from occurred_at /
    # (aggregate_id, sequence_number), not row insertion order.
    store.rows.reverse()
    cold = SharedCodeContextProvider(event_store=store)
    recovered = await cold.code_block(root_id)

    # Then: identical block -- the canonical order is content-addressed, not
    # dependent on how rows happen to come back from the DB.
    assert recovered == expected
