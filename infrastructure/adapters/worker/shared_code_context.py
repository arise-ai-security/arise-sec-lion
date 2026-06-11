"""Shared code-context provider: persist viewed source and serve the cached block.

Concrete :class:`core.ports.shared_code_context_port.SharedCodeContextPort`. Single
responsibility: turn the file contents a worker's ``view`` tool returned into frozen
``SourceFileObserved`` events on that worker's aggregate, and rebuild the canonical,
byte-stable code-prefix block for a run **purely by replaying** those events (plus
``SourceFileEdited``) from the event store. The block is an APPEND-ONLY log: replaying
any prefix of the event sequence renders a byte prefix of every later block, so the
carried content stays inside the OpenAI prefix-cache window across sequential workers.
Source of truth is the DB — no disk reads, no mutable cache of contents — so the exact
bytes injected into any worker's prompt are recoverable from the event store alone
(spec ``docs/superpowers/specs/2026-06-10-shared-code-prefix-cache-design.md``).

Capture is driven by real LLM tool calls; the provider never decides what to read.

Emit sink (DIP/Strategy)
------------------------
``record_view`` / ``record_edit`` build the event via the aggregate's own domain
method (Foundation) and hand it to an **emit sink**:

* default sink → append to the event store on the worker's aggregate, with OCC retry.
  This is the standalone path and the one the recoverability tests exercise.
* the live OpenHands worker injects a sink that enqueues the event for the adapter to
  *yield* into the worker stream, where ``AgentSession.apply_worker_event`` re-sequences
  it on the single in-memory aggregate. That keeps the live path free of the OCC race
  that an out-of-band append to a mid-execution aggregate would cause (Invariant D).

Either way the event lands on the observing worker's aggregate and the block is
recoverable from the DB.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from jinja2 import Environment, FileSystemLoader

from core.domain.aggregates.agent_session import AgentSession
from core.domain.events.events import (
    DomainEvent,
    SourceFileEdited,
    SourceFileObserved,
)


if TYPE_CHECKING:
    from core.ports.event_store_port import EventStorePort


logger = logging.getLogger(__name__)

_TEMPLATE_NAME = "context/shared_code.j2"
_INDEX_TEMPLATE_NAME = "context/shared_code_index.j2"
_DEFAULT_TEMPLATE_ROOT = "prompts"

RenderMode = str  # "append_only" (byte-stable log) | "latest_only" (one entry per path)

# Sink: persist (or otherwise route) the events a single emit produced. Returns
# nothing; the provider's public methods stay ``-> None`` per the port contract.
EmitSink = Callable[[UUID, list[DomainEvent]], Awaitable[None]]


class SharedCodeContextProvider:
    """Records viewed/edited source and serves the run's canonical code block."""

    def __init__(
        self,
        event_store: EventStorePort,
        *,
        template_root: str | Path = _DEFAULT_TEMPLATE_ROOT,
        emit_sink: EmitSink | None = None,
        render_mode: RenderMode = "append_only",
        index_enabled: bool = False,
        _shared_memo: dict[tuple[str, UUID, frozenset[UUID]], str | None] | None = None,
        _shared_env: Environment | None = None,
    ) -> None:
        self._event_store = event_store
        self._emit_sink: EmitSink = emit_sink or self._append_to_event_store
        self._env = _shared_env or Environment(
            loader=FileSystemLoader(str(template_root)),
            autoescape=False,  # noqa: S701 - plain-text prompt, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )
        if render_mode not in ("append_only", "latest_only"):
            raise ValueError(f"Unknown shared-code render_mode: {render_mode!r}")
        # latest_only trades the byte-stable prefix for a smaller block: one
        # entry per path with its newest content. A mid-run re-view rewrites
        # the block, so later-spawned workers lose the prefix-cache hit from
        # that path onward. Both modes stay pure functions of the event set.
        self._render_mode: RenderMode = render_mode
        self._index_enabled = index_enabled
        # Render memo: (kind, root_id, frozenset of event_ids) -> rendered text.
        # The key is the exact set of events the text was built from, so any new
        # view/edit busts it while a no-op rebuild is served from memory. Shared
        # with sibling capture instances (see ``bind_capture``) so cache hits carry.
        self._memo: dict[tuple[str, UUID, frozenset[UUID]], str | None] = (
            _shared_memo if _shared_memo is not None else {}
        )

    async def record_view(
        self, root_id: UUID, agent_id: UUID, path: str, content: str
    ) -> None:
        """Persist the content a worker's ``view`` returned as ``SourceFileObserved``."""
        del root_id  # Event lands on the worker's own aggregate; root scopes only reads.

        def emit(agent: AgentSession) -> None:
            agent.record_source_file_observed(path=path, content=content, observed_by=agent_id)

        await self._emit_on_aggregate(agent_id, emit)

    async def record_edit(self, root_id: UUID, agent_id: UUID, path: str) -> None:
        """Mark a file edited as ``SourceFileEdited`` (stale note until re-viewed)."""
        del root_id

        def emit(agent: AgentSession) -> None:
            agent.record_source_file_edited(path=path, edited_by=agent_id)

        await self._emit_on_aggregate(agent_id, emit)

    async def code_block(self, root_id: UUID) -> str | None:
        """Rebuild the run's canonical block from events (or None)."""
        relevant = await self._source_events(root_id)
        memo_key = ("block", root_id, frozenset(event.event_id for event in relevant))
        if memo_key in self._memo:
            return self._memo[memo_key]

        entries = self._entries_for_mode(relevant)
        block = self._render(entries) if entries else None
        self._memo[memo_key] = block
        return block

    async def code_index(self, root_id: UUID) -> str | None:
        """Compact per-path freshness index of the block, or None when disabled."""
        if not self._index_enabled:
            return None
        relevant = await self._source_events(root_id)
        memo_key = ("index", root_id, frozenset(event.event_id for event in relevant))
        if memo_key in self._memo:
            return self._memo[memo_key]

        entries = self._entries_for_mode(relevant)
        index = self._render_index(self._index_rows(entries)) if entries else None
        self._memo[memo_key] = index
        return index

    async def _source_events(
        self, root_id: UUID
    ) -> list[SourceFileObserved | SourceFileEdited]:
        grouped = await self._event_store.get_hierarchy_events_grouped(root_id)
        return self._collect_source_events(grouped)

    def _entries_for_mode(
        self, events: list[SourceFileObserved | SourceFileEdited]
    ) -> list[dict[str, object]]:
        if self._render_mode == "latest_only":
            return self._compact_to_latest(self._build_entries(events))
        return self._build_entries(events)

    def bind_capture(self, emit_sink: EmitSink) -> SharedCodeContextProvider:
        """Return a sibling provider that routes emission through ``emit_sink``.

        Used by the live OpenHands worker: a fresh-conversation worker holds a
        single in-memory aggregate (the orchestrator's), so an out-of-band append
        to it would break OCC (Invariant D). The adapter instead binds a sink that
        *yields* the built events into the worker stream, where
        ``AgentSession.apply_worker_event`` re-sequences them on that one aggregate.
        Event-construction (and the sha256) stays in one place — the aggregate's
        domain method — so the captured bytes are identical to the standalone path.
        The render memo is shared so ``code_block`` cache hits carry across.
        """
        return SharedCodeContextProvider(
            self._event_store,
            emit_sink=emit_sink,
            render_mode=self._render_mode,
            index_enabled=self._index_enabled,
            _shared_memo=self._memo,
            _shared_env=self._env,
        )

    # ------------------------------------------------------------------ emit

    async def _emit_on_aggregate(
        self, agent_id: UUID, emit: Callable[[AgentSession], None]
    ) -> None:
        agent = await self._load_aggregate(agent_id)
        emit(agent)
        new_events = list(agent.events)
        if not new_events:
            return
        await self._emit_sink(agent_id, new_events)
        agent.mark_changes_as_committed()

    async def _load_aggregate(self, agent_id: UUID) -> AgentSession:
        events = await self._event_store.get_events(agent_id)
        return AgentSession.load_from_history(events)

    async def _append_to_event_store(
        self, agent_id: UUID, events: list[DomainEvent]
    ) -> None:
        del agent_id  # Events already carry their aggregate_id.
        await self._event_store.append_batch(events)

    # ------------------------------------------------------------- rebuild

    @staticmethod
    def _collect_source_events(
        grouped: dict[UUID, list[DomainEvent]],
    ) -> list[SourceFileObserved | SourceFileEdited]:
        """Flatten the run's SourceFile events into one deterministic total order.

        Events are spread across worker aggregates; ``occurred_at`` gives the cross
        aggregate happens-before, with (aggregate_id, sequence_number) as a stable
        tie-break so the order — and thus the rendered block — is byte-reproducible.
        """
        events: list[SourceFileObserved | SourceFileEdited] = [
            event
            for aggregate_events in grouped.values()
            for event in aggregate_events
            if isinstance(event, (SourceFileObserved, SourceFileEdited))
        ]
        events.sort(key=lambda e: (e.occurred_at, e.aggregate_id, e.sequence_number))
        return events

    @staticmethod
    def _build_entries(
        events: list[SourceFileObserved | SourceFileEdited],
    ) -> list[dict[str, object]]:
        """Reduce the ordered events to an APPEND-ONLY entry log.

        Each event appends at most one entry and never rewrites or reorders earlier
        ones, so the block rendered from any event-sequence prefix is a byte prefix
        of every later block — the property OpenAI's prefix cache needs across a
        run's sequential workers. A view appends a full ``file`` revision unless it
        repeats the path's current content; an edit appends one ``note`` entry
        (rendered ``<file_modified>``: the copy above is stale until a newer
        revision appears below). Edits of never-observed paths append nothing.
        """
        entries: list[dict[str, object]] = []
        last_content: dict[str, str] = {}
        revisions: dict[str, int] = {}
        dirty: set[str] = set()

        for event in events:
            path = event.path
            if isinstance(event, SourceFileEdited):
                if path in last_content and path not in dirty:
                    entries.append({"kind": "note", "path": path})
                    dirty.add(path)
                continue
            # SourceFileObserved: skip pure no-op re-views of unmodified content.
            if path not in dirty and last_content.get(path) == event.content:
                continue
            revisions[path] = revisions.get(path, 0) + 1
            entries.append(
                {
                    "kind": "file",
                    "path": path,
                    "content": event.content,
                    "revision": revisions[path],
                }
            )
            last_content[path] = event.content
            dirty.discard(path)
        return entries

    @staticmethod
    def _compact_to_latest(entries: list[dict[str, object]]) -> list[dict[str, object]]:
        """One entry per path: newest content, plus a stale note when still dirty.

        Paths keep their first-seen order so a re-view of an already-listed file
        rewrites the block only from that path's position onward — the prefix
        before it stays byte-identical for later-spawned workers.
        """
        order: list[str] = []
        latest: dict[str, dict[str, object]] = {}
        dirty: set[str] = set()
        for entry in entries:
            path = str(entry["path"])
            if entry["kind"] == "note":
                dirty.add(path)
                continue
            if path not in latest:
                order.append(path)
            latest[path] = entry
            dirty.discard(path)

        compacted: list[dict[str, object]] = []
        for path in order:
            compacted.append(latest[path])
            if path in dirty:
                compacted.append({"kind": "note", "path": path})
        return compacted

    @staticmethod
    def _index_rows(entries: list[dict[str, object]]) -> list[dict[str, object]]:
        """Per-path freshness rows (first-seen order) for the volatile-tail index."""
        order: list[str] = []
        revision: dict[str, object] = {}
        stale: dict[str, bool] = {}
        for entry in entries:
            path = str(entry["path"])
            if entry["kind"] == "note":
                stale[path] = True
                continue
            if path not in revision:
                order.append(path)
            revision[path] = entry["revision"]
            stale[path] = False
        return [
            {"path": path, "revision": revision[path], "stale": stale[path]} for path in order
        ]

    def _render(self, entries: list[dict[str, object]]) -> str:
        return self._env.get_template(_TEMPLATE_NAME).render(entries=entries)

    def _render_index(self, rows: list[dict[str, object]]) -> str | None:
        if not rows:
            return None
        return self._env.get_template(_INDEX_TEMPLATE_NAME).render(rows=rows)
