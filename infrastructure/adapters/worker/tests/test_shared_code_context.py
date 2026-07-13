"""Tests for SharedCodeContextProvider (shared code-prefix cache, infra side).

The provider persists viewed/edited source as SourceFileObserved / SourceFileEdited
events on the observing worker's aggregate and rebuilds the canonical byte-stable
code block purely by replaying those events from the event store. These tests use a
minimal in-memory event store so the rebuild path is exercised end to end.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.application.services.lifecycle.child_factory import ChildAgentFactory
from core.application.services.lifecycle.hierarchy_limits_registry import (
    HierarchyLimitsRegistry,
)
from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    DomainEvent,
    SourceFileEdited,
    SourceFileObserved,
)
from core.domain.values.node_message import Briefing
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
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
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
    # A bare view returns the file in full: the default capture is "full", not a
    # conservative "truncated" (Bug 2 — the old default mislabeled full reads).
    assert observed[0].capture_type == "full"


@pytest.mark.asyncio
async def test_record_view_records_full_capture_with_real_range() -> None:
    """A verified full-file view is stored capture_type='full' with its real range."""

    # Given: a provider over an in-memory store and a single worker == root
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    content = "line1\nline2\nline3\n"

    # When: the worker records a full-file view with its actual line span
    await provider.record_view(
        root_id,
        root_id,
        "/src/full.c",
        content,
        capture_type="full",
        actual_start_line=1,
        actual_end_line=3,
        phase="reproduce",
        role="worker",
    )

    # Then: the observation is stored as a full capture with the real range
    observed = [e for e in await store.get_events(root_id) if isinstance(e, SourceFileObserved)]
    assert len(observed) == 1
    assert observed[0].capture_type == "full"
    assert observed[0].actual_start_line == 1
    assert observed[0].actual_end_line == 3
    assert observed[0].phase == "reproduce"
    assert observed[0].role == "worker"


@pytest.mark.asyncio
async def test_record_view_records_partial_capture_with_requested_and_actual_range() -> None:
    """A ranged/truncated view is stored partial with both requested and actual range."""

    # Given: a provider over an in-memory store and a single worker == root
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)

    # When: the worker records a range excerpt (requested 40-80, got 40-80)
    await provider.record_view(
        root_id,
        root_id,
        "/src/big.c",
        "excerpt body\n",
        capture_type="range",
        requested_start_line=40,
        requested_end_line=80,
        actual_start_line=40,
        actual_end_line=80,
    )

    # Then: the observation is a range capture carrying requested + actual span
    observed = [e for e in await store.get_events(root_id) if isinstance(e, SourceFileObserved)]
    assert len(observed) == 1
    assert observed[0].capture_type == "range"
    assert observed[0].requested_start_line == 40
    assert observed[0].requested_end_line == 80
    assert observed[0].actual_start_line == 40
    assert observed[0].actual_end_line == 80


@pytest.mark.asyncio
async def test_context_packet_is_targeted_deterministic_and_budget_bounded() -> None:
    """Scoped packets prioritize targets and explicitly index omissions."""

    # Given: Six current source observations plus a generated log
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    for index in range(6):
        await provider.record_view(
            root_id,
            root_id,
            f"/src/file_{index}.c",
            f"void symbol_{index}(void) {{}}\n",
        )
    await provider.record_view(root_id, root_id, "/src/build/run.log", "raw log")

    # When: A worker requests one exact target and symbol
    first = await provider.context_packet(
        root_id,
        target_paths=("/src/file_5.c",),
        symbols=("symbol_4",),
    )
    second = await provider.context_packet(
        root_id,
        target_paths=("/src/file_5.c",),
        symbols=("symbol_4",),
    )

    # Then: Selection is stable, bounded, prioritized, and omissions are visible.
    # Priority is SYMBOL before PATH (Bug 3): the symbol_4 match (file_4) outranks
    # the path target (file_5).
    assert first == second
    assert len(first.entries) == 4
    assert first.total_tokens <= 16_000
    assert first.entries[0]["path"] == "/src/file_4.c"
    assert first.entries[1]["path"] == "/src/file_5.c"
    assert first.source_index is not None
    assert "OMITTED" in first.source_index
    assert "/src/build/run.log" in first.source_index


@pytest.mark.asyncio
async def test_range_excerpt_never_rendered_as_full_file() -> None:
    """A range excerpt is marked partial with its span, never a full <file> (Bug 1)."""

    # Given: a whole-file view and a 40-80 excerpt of a large file
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(
        root_id, root_id, "/src/whole.c", "FULL BODY\n",
        capture_type="full", actual_start_line=1, actual_end_line=1,
    )
    await provider.record_view(
        root_id, root_id, "/src/big.c", "EXCERPT BODY\n",
        capture_type="range",
        requested_start_line=40, requested_end_line=80,
        actual_start_line=40, actual_end_line=80,
    )

    # When: the packet block is rendered
    block = (await provider.context_packet(root_id)).source_block

    # Then: the full file is provided as a full <file>; the excerpt is NEVER a full
    # <file> — it renders as a distinct <file_excerpt> carrying its line range.
    assert block is not None
    assert '<file path="/src/whole.c" revision="' in block
    assert '<file path="/src/big.c"' not in block
    assert "<file_excerpt" in block
    assert '"/src/big.c"' in block
    assert 'lines="40-80"' in block


@pytest.mark.asyncio
async def test_symbol_target_ordered_before_path_target() -> None:
    """Selection priority: exact SYMBOL target outranks exact PATH target (Bug 3)."""

    # Given: one file matched by symbol, one matched by path
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(
        root_id, root_id, "/src/by_symbol.c", "void target_symbol(void) {}\n"
    )
    await provider.record_view(root_id, root_id, "/src/by_path.c", "void other(void) {}\n")

    # When: a worker requests one exact path target and one exact symbol target
    packet = await provider.context_packet(
        root_id,
        target_paths=("/src/by_path.c",),
        symbols=("target_symbol",),
    )

    # Then: the symbol-target entry is selected/ordered ahead of the path-target
    assert packet.entries[0]["path"] == "/src/by_symbol.c"
    assert packet.entries[1]["path"] == "/src/by_path.c"


@pytest.mark.asyncio
async def test_packet_index_bounded_by_metadata_budget() -> None:
    """The metadata/evidence index is bounded and records its overflow (Bug 4)."""

    # Given: many source files (few selected, a long omitted tail) and a tight
    # metadata budget for the index/evidence segment.
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(
        event_store=store, scoped_packets=True, metadata_token_budget=300
    )
    for i in range(200):
        await provider.record_view(
            root_id, root_id, f"/src/file_{i:03d}.c", f"void s_{i}(void) {{}}\n"
        )

    # When: the packet is assembled
    packet = await provider.context_packet(root_id)

    # Then: the index fits its metadata budget, and the clipped remainder is
    # recorded rather than silently overflowing.
    assert packet.source_index is not None
    index_tokens = (len(packet.source_index) + 3) // 4
    assert index_tokens <= 300
    assert "metadata_budget" in packet.source_index


@pytest.mark.asyncio
async def test_context_packet_excludes_stale_revision_after_edit() -> None:
    """An edited path is absent until a newer observation is captured."""

    # Given: A viewed source revision that is subsequently edited
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(root_id, root_id, "/src/a.c", "old\n")
    await provider.record_edit(root_id, root_id, "/src/a.c")

    # When: A packet is assembled before and after a fresh observation
    stale_packet = await provider.context_packet(root_id)
    await provider.record_view(root_id, root_id, "/src/a.c", "new\n")
    fresh_packet = await provider.context_packet(root_id)

    # Then: Stale content is excluded and only the current revision returns
    assert stale_packet.source_block is None
    assert fresh_packet.source_block is not None
    assert "new" in fresh_packet.source_block
    assert "old" not in fresh_packet.source_block


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
async def test_predecessor_scope_includes_predecessor_and_excludes_unrelated_sibling() -> None:
    """Dependency scope keeps a hard predecessor's source and drops an unrelated sibling."""

    # Given: a predecessor and an unrelated sibling each observe a distinct file
    store = _InMemoryEventStore()
    root_id = uuid4()
    predecessor = uuid4()
    sibling = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=predecessor)
    await _seed_worker(store, root_id=root_id, agent_id=sibling)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(root_id, predecessor, "/src/pred.c", "void from_pred(void) {}\n")
    await provider.record_view(root_id, sibling, "/src/unrelated.c", "void from_sib(void) {}\n")

    # When: the consumer scopes the packet to the hard predecessor only
    packet = await provider.context_packet(root_id, hard_predecessor_ids=(predecessor,))

    # Then: the predecessor's observation is included; the sibling's is excluded
    paths = [entry["path"] for entry in packet.entries]
    assert "/src/pred.c" in paths
    assert "/src/unrelated.c" not in paths
    # And: it is tagged as the (previously-unreachable) required-predecessor-artifact tier
    assert packet.entries[0]["inclusion_reason"] == "required_predecessor_artifact"
    # And: the excluded sibling is recorded as out-of-scope, not silently dropped
    assert packet.source_index is not None
    assert "/src/unrelated.c" in packet.source_index
    assert "out_of_scope" in packet.source_index


@pytest.mark.asyncio
async def test_direct_and_grouped_entry_children_receive_equivalent_scoped_packets() -> None:
    # Given: equivalent direct and grouped entry children with different concrete producers
    factory = ChildAgentFactory(
        repository=AsyncMock(),
        limits_registry=HierarchyLimitsRegistry(),
    )
    agent_config = {
        "strategy": "heuristic",
        "base": {"model": "test-model", "temperature": 0.0, "max_tokens": 1000},
        "tool": "openhands",
    }
    direct_parent, direct_producer, direct_entry = uuid4(), uuid4(), uuid4()
    grouped_parent, grouped_producer, grouped_source, grouped_sink, grouped_entry = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    direct_events = [
        ChildSpawned(
            aggregate_id=direct_parent,
            sequence_number=1,
            child_id=direct_producer,
            child_role="worker",
            child_config=agent_config,
            sibling_index=0,
            subtask=Subtask(description="producer", config=agent_config),
            briefing=Briefing.simple("direct").model_dump(),
        ),
        ChildSpawned(
            aggregate_id=direct_parent,
            sequence_number=2,
            child_id=direct_entry,
            child_role="worker",
            child_config=agent_config,
            sibling_index=1,
            subtask=Subtask(
                description="entry", config=agent_config, depends_on=[0]
            ),
            briefing=Briefing.simple("direct").model_dump(),
        ),
    ]
    grouped_producer_events = [
        ChildSpawned(
            aggregate_id=grouped_producer,
            sequence_number=2,
            child_id=grouped_source,
            child_role="worker",
            child_config=agent_config,
            sibling_index=0,
            subtask=Subtask(description="source", config=agent_config),
            briefing=Briefing.simple("producer group").model_dump(),
        ),
        ChildSpawned(
            aggregate_id=grouped_producer,
            sequence_number=3,
            child_id=grouped_sink,
            child_role="worker",
            child_config=agent_config,
            sibling_index=1,
            subtask=Subtask(
                description="sink", config=agent_config, depends_on=[0]
            ),
            briefing=Briefing.simple("producer group").model_dump(),
        ),
    ]
    grouped_consumer_events = [
        ChildSpawned(
            aggregate_id=grouped_parent,
            sequence_number=1,
            child_id=grouped_entry,
            child_role="worker",
            child_config=agent_config,
            sibling_index=0,
            subtask=Subtask(description="entry", config=agent_config),
            briefing=Briefing.simple("grouped").model_dump(),
        )
    ]
    direct_results = await factory.create_children_from_events(
        direct_events, direct_parent
    )
    await factory.create_children_from_events(
        grouped_producer_events, grouped_producer
    )
    grouped_results = await factory.create_children_from_events(
        grouped_consumer_events,
        grouped_parent,
        parent_hard_predecessor_ids=(grouped_producer,),
    )
    direct_child = next(
        result.agent for result in direct_results if result.agent.agent_id == direct_entry
    )
    grouped_child = grouped_results[0].agent
    assert direct_child.hard_predecessor_ids == [direct_producer]
    assert grouped_child.hard_predecessor_ids == [grouped_sink]

    direct_store = _InMemoryEventStore()
    grouped_store = _InMemoryEventStore()
    await _seed_worker(direct_store, root_id=direct_parent, agent_id=direct_parent)
    await _seed_worker(direct_store, root_id=direct_parent, agent_id=direct_producer)
    await _seed_worker(grouped_store, root_id=grouped_parent, agent_id=grouped_parent)
    await grouped_store.append(
        AgentCreated(
            aggregate_id=grouped_producer,
            sequence_number=1,
            role="manager",
            parent_id=grouped_parent,
            config=agent_config,
        )
    )
    await grouped_store.append(
        ChildSpawned(
            aggregate_id=grouped_parent,
            sequence_number=2,
            child_id=grouped_producer,
            child_role="manager",
            subtask=Subtask(description="producer group", config=agent_config),
            child_config=agent_config,
        )
    )
    for child_id in (grouped_source, grouped_sink):
        await grouped_store.append(
            AgentCreated(
                aggregate_id=child_id,
                sequence_number=1,
                role="worker",
                parent_id=grouped_producer,
                config=agent_config,
            )
        )
    await grouped_store.append_batch(grouped_producer_events)
    direct_provider = SharedCodeContextProvider(
        event_store=direct_store, scoped_packets=True
    )
    grouped_provider = SharedCodeContextProvider(
        event_store=grouped_store, scoped_packets=True
    )
    content = "int upstream(void) { return 1; }\n"
    await direct_provider.record_view(
        direct_parent, direct_producer, "/src/upstream.c", content
    )
    await grouped_provider.record_view(
        grouped_parent, grouped_sink, "/src/upstream.c", content
    )

    # When: each entry child requests its dependency-scoped packet
    direct_packet = await direct_provider.context_packet(
        direct_parent,
        hard_predecessor_ids=tuple(direct_child.hard_predecessor_ids),
    )
    grouped_packet = await grouped_provider.context_packet(
        grouped_parent,
        hard_predecessor_ids=tuple(grouped_child.hard_predecessor_ids),
    )

    # Then: omitting one grouping layer does not change the packet contents
    assert direct_packet == grouped_packet
    assert [entry["path"] for entry in direct_packet.entries] == ["/src/upstream.c"]


@pytest.mark.asyncio
async def test_consumed_artifact_scope_selects_referenced_path_over_noise() -> None:
    """A consumed-artifact path reference selects that path and excludes noise."""

    # Given: two workers observe files; only one path is a consumed contract artifact
    store = _InMemoryEventStore()
    root_id = uuid4()
    producer = uuid4()
    other = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=producer)
    await _seed_worker(store, root_id=root_id, agent_id=other)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(root_id, producer, "/src/contract.c", "artifact body\n")
    await provider.record_view(root_id, other, "/src/noise.c", "noise body\n")

    # When: the consumer scopes by a consumed-artifact path reference (no predecessor id)
    packet = await provider.context_packet(
        root_id, consumed_artifacts=("/src/contract.c",)
    )

    # Then: only the artifact-referenced path is selected, tagged predecessor-artifact
    assert [entry["path"] for entry in packet.entries] == ["/src/contract.c"]
    assert packet.entries[0]["inclusion_reason"] == "required_predecessor_artifact"


@pytest.mark.asyncio
async def test_phase_scope_excludes_foreign_phase_but_keeps_explicit_target() -> None:
    """Phase scope keeps same-phase evidence and explicit targets, drops foreign phases."""

    # Given: one same-phase file, one foreign-phase file, and a foreign-phase target
    store = _InMemoryEventStore()
    root_id = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(root_id, root_id, "/src/repro_local.c", "a\n", phase="reproduce")
    await provider.record_view(root_id, root_id, "/src/analysis.c", "b\n", phase="analyze")
    await provider.record_view(root_id, root_id, "/src/target.c", "c\n", phase="analyze")

    # When: the consumer scopes to the "reproduce" phase but names /src/target.c a target
    packet = await provider.context_packet(
        root_id, phase="reproduce", target_paths=("/src/target.c",)
    )

    # Then: same-phase evidence and the explicit target are kept; the foreign-phase,
    # non-target file is excluded as out-of-scope
    paths = {entry["path"] for entry in packet.entries}
    assert "/src/repro_local.c" in paths
    assert "/src/target.c" in paths
    assert "/src/analysis.c" not in paths


@pytest.mark.asyncio
async def test_no_scope_inputs_leave_packet_unfiltered() -> None:
    """Back-compat: with no dependency-scope inputs, no observation is filtered out."""

    # Given: two workers observe two files (an unrelated sibling among them)
    store = _InMemoryEventStore()
    root_id = uuid4()
    sibling = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=sibling)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    await provider.record_view(root_id, root_id, "/src/a.c", "a\n")
    await provider.record_view(root_id, sibling, "/src/b.c", "b\n")

    # When: no dependency-scope inputs are supplied
    packet = await provider.context_packet(root_id)

    # Then: both observations remain (no filtering) and none is marked out_of_scope
    assert {entry["path"] for entry in packet.entries} == {"/src/a.c", "/src/b.c"}
    assert "out_of_scope" not in (packet.source_index or "")


@pytest.mark.asyncio
async def test_scoped_packet_is_deterministic_across_calls() -> None:
    """A dependency-scoped packet is byte-identical across repeated builds."""

    # Given: a predecessor with several files and an unrelated sibling with several files
    store = _InMemoryEventStore()
    root_id = uuid4()
    predecessor = uuid4()
    sibling = uuid4()
    await _seed_worker(store, root_id=root_id, agent_id=root_id)
    await _seed_worker(store, root_id=root_id, agent_id=predecessor)
    await _seed_worker(store, root_id=root_id, agent_id=sibling)
    provider = SharedCodeContextProvider(event_store=store, scoped_packets=True)
    for i in range(3):
        await provider.record_view(
            root_id, predecessor, f"/src/pred_{i}.c", f"void p{i}(void) {{}}\n"
        )
    for i in range(3):
        await provider.record_view(
            root_id, sibling, f"/src/sib_{i}.c", f"void s{i}(void) {{}}\n"
        )

    # When: the same predecessor-scoped packet is built twice
    first = await provider.context_packet(root_id, hard_predecessor_ids=(predecessor,))
    second = await provider.context_packet(root_id, hard_predecessor_ids=(predecessor,))

    # Then: identical, and scoped to exactly the predecessor's files
    assert first == second
    assert {entry["path"] for entry in first.entries} == {
        "/src/pred_0.c",
        "/src/pred_1.c",
        "/src/pred_2.c",
    }


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
