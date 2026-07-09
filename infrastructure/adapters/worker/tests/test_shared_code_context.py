"""Tests for SharedCodeContextProvider (shared code-prefix cache, infra side).

The provider persists viewed/edited source as SourceFileObserved / SourceFileEdited
events on the observing worker's aggregate and rebuilds the canonical byte-stable
code block purely by replaying those events from the event store. These tests use a
minimal in-memory event store so the rebuild path is exercised end to end.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    DomainEvent,
    SourceFileEdited,
    SourceFileObserved,
)
from core.domain.values.subtask import Subtask
from infrastructure.adapters.worker.shared_code_context import SharedCodeContextProvider


class _InMemoryEventStore:
    """Smallest event store the provider needs: per-aggregate append + hierarchy read.

    ``get_hierarchy_events_grouped`` mirrors the Postgres recursive CTE: the root
    aggregate plus every descendant reachable via ChildSpawned. OCC is enforced the
    same way the real store does — a duplicate (aggregate_id, sequence_number) raises.
    """

    def __init__(self) -> None:
        self._events: dict[UUID, list[DomainEvent]] = {}

    async def append(self, event: DomainEvent) -> None:
        bucket = self._events.setdefault(event.aggregate_id, [])
        if any(e.sequence_number == event.sequence_number for e in bucket):
            raise ValueError(
                f"OCC conflict: ({event.aggregate_id}, {event.sequence_number}) exists"
            )
        bucket.append(event)

    async def append_batch(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.append(event)

    async def get_events(
        self,
        aggregate_id: UUID,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> list[DomainEvent]:
        events = sorted(
            self._events.get(aggregate_id, []), key=lambda e: e.sequence_number
        )
        if after_sequence is not None:
            events = [e for e in events if e.sequence_number > after_sequence]
        if limit is not None:
            events = events[:limit]
        return events

    async def get_hierarchy_events_grouped(
        self, root_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        reachable: set[UUID] = {root_id}
        frontier = [root_id]
        while frontier:
            current = frontier.pop()
            for event in self._events.get(current, []):
                if isinstance(event, ChildSpawned):
                    child = event.child_id
                    if child not in reachable:
                        reachable.add(child)
                        frontier.append(child)
        return {
            agg: sorted(events, key=lambda e: e.sequence_number)
            for agg, events in self._events.items()
            if agg in reachable
        }


async def _seed_worker(store: _InMemoryEventStore, *, root_id: UUID, agent_id: UUID) -> None:
    """Put a minimal AgentCreated on a worker so it is a real aggregate.

    The root links to the worker via a ChildSpawned so the hierarchy read reaches
    the worker's SourceFile events. When ``agent_id == root_id`` (single-worker run)
    only the AgentCreated is needed.
    """
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
        # A ChildSpawned on the root makes the worker reachable via the CTE.
        root_events = store._events.get(root_id, [])
        next_seq = max((e.sequence_number for e in root_events), default=0) + 1
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


@pytest.mark.asyncio
async def test_record_view_emits_source_file_observed_with_sha256() -> None:
    # Given: a provider over an in-memory store and a single worker == root
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store)
    content = "int main() { return 0; }\n"

    # When: the worker records a viewed file
    await provider.record_view(root_id, root_id, "/src/main.c", content)

    # Then: a SourceFileObserved is persisted on the worker aggregate with the
    # verbatim content and its sha256, recoverable from the store alone.
    events = await store.get_events(root_id)
    observed = [e for e in events if isinstance(e, SourceFileObserved)]
    assert len(observed) == 1
    assert observed[0].path == "/src/main.c"
    assert observed[0].content == content
    assert observed[0].content_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert observed[0].observed_by == str(root_id)


@pytest.mark.asyncio
async def test_code_block_renders_single_viewed_file() -> None:
    # Given: one worker that viewed one file
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store)
    await provider.record_view(root_id, root_id, "/src/a.c", "AAA\n")

    # When: the canonical block is rebuilt from events
    block = await provider.code_block(root_id)

    # Then: it contains the file verbatim inside the provided-files wrapper
    assert block is not None
    assert "<provided_source_files>" in block
    assert '<file path="/src/a.c" revision="1">' in block
    assert "AAA" in block


@pytest.mark.asyncio
async def test_code_block_none_when_no_views() -> None:
    # Given: a run with no observed source
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store)

    # When/Then: the block is None (nothing to inject)
    assert await provider.code_block(root_id) is None


@pytest.mark.asyncio
async def test_edit_appends_stale_note_and_keeps_prior_revision() -> None:
    # Given: a run where two workers view files, then one file is edited
    store = _InMemoryEventStore()
    root_id = uuid4()
    worker_b = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=worker_b)
    provider = SharedCodeContextProvider(event_store=store)

    # a.c and b.c observed by the root worker
    await provider.record_view(root_id, root_id, "/src/a.c", "A-v1\n")
    await provider.record_view(root_id, root_id, "/src/b.c", "B-v1\n")
    # worker B edits b.c (no later view) -> append-only: the v1 copy STAYS and a
    # <file_modified> stale note is appended after it (earlier bytes never change)
    await provider.record_edit(root_id, worker_b, "/src/b.c")

    # When
    block = await provider.code_block(root_id)

    # Then: both files present; the edit appended a stale note after b.c's copy
    assert block is not None
    assert '<file path="/src/a.c" revision="1">' in block
    assert "A-v1" in block
    assert '<file path="/src/b.c" revision="1">' in block
    assert "B-v1" in block
    note = '<file_modified path="/src/b.c"/>'
    assert note in block
    assert block.index(note) > block.index("B-v1")


@pytest.mark.asyncio
async def test_reobserved_after_edit_appends_new_revision_after_stale_note() -> None:
    # Given: a.c then b.c observed; b.c edited then re-observed with new bytes
    store = _InMemoryEventStore()
    root_id = uuid4()
    worker_b = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=worker_b)
    provider = SharedCodeContextProvider(event_store=store)

    await provider.record_view(root_id, root_id, "/src/a.c", "A-v1\n")
    await provider.record_view(root_id, root_id, "/src/b.c", "B-v1\n")
    await provider.record_edit(root_id, worker_b, "/src/b.c")
    await provider.record_view(root_id, worker_b, "/src/b.c", "B-v2-EDITED\n")

    # When
    block = await provider.code_block(root_id)

    # Then: append-only log — v1 copy, stale note, then the v2 revision, in order;
    # the LAST revision carries the post-edit bytes.
    assert block is not None
    rev1 = '<file path="/src/b.c" revision="1">'
    note = '<file_modified path="/src/b.c"/>'
    rev2 = '<file path="/src/b.c" revision="2">'
    assert block.index(rev1) < block.index(note) < block.index(rev2)
    assert "B-v1" in block
    assert "B-v2-EDITED" in block
    assert block.index("B-v1") < block.index("B-v2-EDITED")


_CLOSER = "</provided_source_files>"


def _appendable_prefix(block: str) -> str:
    """The block minus its closing tag — the part every later block must extend."""
    head, sep, tail = block.rpartition(_CLOSER)
    assert sep == _CLOSER
    assert tail.strip() == ""
    return head


@pytest.mark.asyncio
async def test_block_grows_append_only_across_event_sequence() -> None:
    # Given: a run whose source events land one at a time
    store = _InMemoryEventStore()
    root_id = uuid4()
    worker_b = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=worker_b)
    provider = SharedCodeContextProvider(event_store=store)

    async def snapshot() -> str:
        block = await provider.code_block(root_id)
        assert block is not None
        return block

    # When: views, a no-op re-view, edits (one duplicate), and a re-observation
    blocks = []
    await provider.record_view(root_id, root_id, "/src/a.c", "A-v1\n")
    blocks.append(await snapshot())
    await provider.record_view(root_id, root_id, "/src/b.c", "B-v1\n")
    blocks.append(await snapshot())
    await provider.record_view(root_id, worker_b, "/src/a.c", "A-v1\n")  # no-op re-view
    blocks.append(await snapshot())
    await provider.record_edit(root_id, worker_b, "/src/a.c")
    blocks.append(await snapshot())
    await provider.record_edit(root_id, worker_b, "/src/a.c")  # duplicate edit: no entry
    blocks.append(await snapshot())
    await provider.record_view(root_id, worker_b, "/src/a.c", "A-v2\n")
    blocks.append(await snapshot())

    # Then: every earlier block is a byte prefix of every later one (modulo the
    # closing tag), so each worker's request shares the longest possible prefix
    # with its predecessor's — the property the OpenAI prefix cache needs.
    for earlier, later in zip(blocks, blocks[1:]):
        assert later.startswith(_appendable_prefix(earlier))
    # And: the no-op re-view and the duplicate edit changed nothing.
    assert blocks[1] == blocks[2]
    assert blocks[3] == blocks[4]


@pytest.mark.asyncio
async def test_block_byte_stable_across_calls_and_memoized() -> None:
    # Given: a fixed set of observed files
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store)
    await provider.record_view(root_id, root_id, "/src/a.c", "A\n")
    await provider.record_view(root_id, root_id, "/src/b.c", "B\n")

    # When: rebuilt twice with no intervening events
    first = await provider.code_block(root_id)
    second = await provider.code_block(root_id)

    # Then: byte-identical (memoized by the event-sequence set)
    assert first is not None
    assert first == second


@pytest.mark.asyncio
async def test_recoverability_block_rebuilds_from_event_store_alone() -> None:
    # Given: a run is simulated, then a BRAND-NEW provider is built over the
    # same store with no in-memory state carried over (recoverability check).
    store = _InMemoryEventStore()
    root_id = uuid4()
    worker_b = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=worker_b)
    writer = SharedCodeContextProvider(event_store=store)
    await writer.record_view(root_id, root_id, "/src/a.c", "A\n")
    await writer.record_view(root_id, worker_b, "/src/b.c", "B\n")
    expected = await writer.code_block(root_id)

    # When: a fresh provider (cold memo) rebuilds purely from the event store
    cold = SharedCodeContextProvider(event_store=store)
    rebuilt = await cold.code_block(root_id)

    # Then: identical bytes — the injected block is fully recoverable from the DB
    assert rebuilt == expected
