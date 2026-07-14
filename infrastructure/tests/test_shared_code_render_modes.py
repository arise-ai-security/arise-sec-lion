"""Shared code block render modes and the provided-files index.

``append_only`` (default) keeps every revision — the byte-stable prefix the
OpenAI cache needs. ``latest_only`` compacts to one entry per path (newest
content, first-seen order) so stale revisions stop riding along every turn; the
trade is a mid-run rewrite when a file is re-viewed. ``code_index`` renders a
compact freshness index for the volatile prompt tail; disabled => None so the
prompt stays byte-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.domain.events.events import DomainEvent, SourceFileEdited, SourceFileObserved
from infrastructure.adapters.worker.shared_code_context import SharedCodeContextProvider


def _prompts_dir() -> Path:
    local_path = Path(__file__).resolve().parents[2] / "prompts"
    return local_path if local_path.exists() else Path("prompts")


class _FakeEventStore:
    def __init__(self, events: list[DomainEvent]) -> None:
        self._events = events

    async def get_hierarchy_events_grouped(self, root_id: UUID) -> dict[UUID, list[DomainEvent]]:
        return {root_id: list(self._events)}

    async def get_events(self, agent_id: UUID) -> list[DomainEvent]:  # pragma: no cover
        return []

    async def append_batch(self, events: list[DomainEvent]) -> None:  # pragma: no cover
        return None


_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _observed(agg: UUID, seq: int, path: str, content: str) -> SourceFileObserved:
    return SourceFileObserved(
        aggregate_id=agg,
        sequence_number=seq,
        occurred_at=_T0 + timedelta(seconds=seq),
        path=path,
        content=content,
        content_sha256="x" * 64,
        observed_by=str(agg),
        # These fixtures represent whole-file views; render them as full <file>
        # entries (the truthful capture a rangeless view now records).
        capture_type="full",
    )


def _edited(agg: UUID, seq: int, path: str) -> SourceFileEdited:
    return SourceFileEdited(
        aggregate_id=agg,
        sequence_number=seq,
        occurred_at=_T0 + timedelta(seconds=seq),
        path=path,
        edited_by=str(agg),
    )


def _events(agg: UUID) -> list[DomainEvent]:
    """a.c viewed twice (two revisions), b.c viewed once then edited (dirty)."""
    return [
        _observed(agg, 1, "/src/a.c", "int a_v1;"),
        _observed(agg, 2, "/src/b.c", "int b_v1;"),
        _observed(agg, 3, "/src/a.c", "int a_v2;"),
        _edited(agg, 4, "/src/b.c"),
    ]


def _provider(events: list[DomainEvent], **kwargs: object) -> SharedCodeContextProvider:
    return SharedCodeContextProvider(
        _FakeEventStore(events),  # type: ignore[arg-type]
        template_root=_prompts_dir(),
        **kwargs,  # type: ignore[arg-type]
    )


class TestLatestOnlyMode:
    @pytest.mark.asyncio
    async def test_append_only_keeps_every_revision(self) -> None:
        # Given
        agg = uuid4()
        provider = _provider(_events(agg))

        # When
        block = await provider.code_block(uuid4())

        # Then: both revisions of a.c are present, plus b.c and its stale note
        assert block is not None
        assert "int a_v1;" in block
        assert "int a_v2;" in block
        assert '<file path="/src/a.c" revision="2">' in block
        assert '<file_modified path="/src/b.c"/>' in block

    @pytest.mark.asyncio
    async def test_latest_only_renders_one_entry_per_path(self) -> None:
        # Given
        agg = uuid4()
        provider = _provider(_events(agg), render_mode="latest_only")

        # When
        block = await provider.code_block(uuid4())

        # Then: stale revision gone, newest content kept with its revision number,
        # first-seen path order preserved, dirty path keeps its stale note
        assert block is not None
        assert "int a_v1;" not in block
        assert "int a_v2;" in block
        assert '<file path="/src/a.c" revision="2">' in block
        assert '<file_modified path="/src/b.c"/>' in block
        assert block.index("/src/a.c") < block.index("/src/b.c")

    def test_unknown_render_mode_is_rejected(self) -> None:
        # Given / When / Then
        with pytest.raises(ValueError, match="render_mode"):
            _provider([], render_mode="diff")


class TestCodeIndex:
    @pytest.mark.asyncio
    async def test_disabled_index_is_none(self) -> None:
        # Given: default construction (index off)
        provider = _provider(_events(uuid4()))

        # When / Then
        assert await provider.code_index(uuid4()) is None

    @pytest.mark.asyncio
    async def test_index_lists_paths_revisions_and_freshness(self) -> None:
        # Given
        provider = _provider(_events(uuid4()), index_enabled=True)

        # When
        index = await provider.code_index(uuid4())

        # Then: data-only rows — current vs stale, no file bodies
        assert index is not None
        assert "<provided_files_index>" in index
        assert "/src/a.c (revision 2, current)" in index
        assert "stale" in index
        assert "/src/b.c" in index
        assert "int a_v2;" not in index

    @pytest.mark.asyncio
    async def test_no_events_renders_no_index(self) -> None:
        # Given
        provider = _provider([], index_enabled=True)

        # When / Then
        assert await provider.code_index(uuid4()) is None

    @pytest.mark.asyncio
    async def test_bind_capture_carries_mode_and_index_flag(self) -> None:
        # Given: a latest_only provider with the index enabled
        agg = uuid4()
        provider = _provider(_events(agg), render_mode="latest_only", index_enabled=True)

        async def _sink(agent_id: UUID, events: list[DomainEvent]) -> None:
            return None

        # When: binding a capture sibling (the live-worker path)
        bound = provider.bind_capture(_sink)
        block = await bound.code_block(uuid4())
        index = await bound.code_index(uuid4())

        # Then: the sibling renders with the same mode and index setting
        assert block is not None and "int a_v1;" not in block
        assert index is not None and "/src/a.c (revision 2, current)" in index
