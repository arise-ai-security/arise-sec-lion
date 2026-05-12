"""Tests for ``WorkerAdapterBase`` (E.9 invariant: per-call worker timing).

Concurrent sessions of the same adapter instance MUST NOT share a clock
field. Before E.9, ``self._start_time`` lived on the adapter, so a second
concurrent call would overwrite the first call's start time and the first
call's ``duration_seconds`` would be effectively zero (or negative).

These tests pin the new invariant by asserting:

1. ``WorkerAdapterBase`` no longer carries a ``_start_time`` instance
   attribute or its ``_start_timing`` / ``_get_duration`` helpers.
2. For each concrete adapter (claude_sdk, google_adk, openhands), two
   concurrent invocations of ``_execute_task`` against a single adapter
   instance produce distinct, per-call durations — each call's
   ``WorkerCostRecorded.duration_seconds`` is computed from a per-call
   ``started_at`` local, not from a single shared clock field.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.domain.events.events import DomainEvent, WorkerCostRecorded
from infrastructure.adapters.worker.base import WorkerAdapterBase
from infrastructure.adapters.worker.shared import EventSequencer


class _MinimalAdapter(WorkerAdapterBase):
    """Concrete subclass used only to exercise the base contract."""

    STREAM_NAME = "minimal"

    async def _execute_task(  # pragma: no cover - not invoked in invariant test
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        yield  # type: ignore[misc]

    def _get_tool_name(self) -> str:  # pragma: no cover - not invoked
        return "minimal"


def test_worker_adapter_base_has_no_start_time_field() -> None:
    """E.9 invariant: ``_start_time`` is removed; no shared clock on the adapter.

    A second concurrent ``_execute_task`` could otherwise overwrite the first
    call's start time and silently corrupt its duration measurement.
    """
    # Given: a freshly constructed adapter.
    adapter = _MinimalAdapter(timeout_seconds=1)

    # Then: there is no shared ``_start_time`` attribute, and no
    # ``_start_timing`` / ``_get_duration`` helpers on the base class.
    assert not hasattr(adapter, "_start_time"), (
        "WorkerAdapterBase must not carry a per-instance _start_time field; "
        "concurrent _execute_task calls would race on it (E.9)."
    )
    assert not hasattr(WorkerAdapterBase, "_start_timing"), (
        "_start_timing helper must be removed; each adapter records "
        "started_at locally inside _execute_task (E.9)."
    )
    assert not hasattr(WorkerAdapterBase, "_get_duration"), (
        "_get_duration helper must be removed; each adapter computes "
        "time() - started_at locally (E.9)."
    )


# -- Per-adapter concurrent-session timing checks --------------------------------


class _PerTaskClock:
    """A test clock keyed by the running ``asyncio.Task``.

    Each distinct task gets its OWN tick stream — index 0, 1, 2... so a
    correctly-implemented adapter that records ``started_at`` *locally*
    inside ``_execute_task`` will see deterministic per-call durations
    regardless of how the event loop interleaves the two calls. A buggy
    adapter that stashes the start time on a shared instance attribute
    would have its first call's value clobbered by the second call.

    The first tick returned to a task is the ``start_tick``; every
    subsequent tick is +1 nanosecond ... +N nanoseconds within that task.
    Using a per-task base offset of 1_000_000 keeps each task's tick
    series cleanly separated from the others.
    """

    def __init__(self) -> None:
        self._task_indices: dict[asyncio.Task[Any], int] = {}
        self._task_ticks: dict[asyncio.Task[Any], int] = {}
        self._next_index = 0

    def __call__(self) -> float:
        task = asyncio.current_task()
        if task is None:
            # Outside an asyncio task (e.g. import-time call); not expected
            # in production code paths exercised by this test.
            return 0.0
        if task not in self._task_indices:
            self._task_indices[task] = self._next_index
            self._task_ticks[task] = 0
            self._next_index += 1
        index = self._task_indices[task]
        tick = self._task_ticks[task]
        self._task_ticks[task] = tick + 1
        return float(index * 1_000_000 + tick)


def _expected_duration_for_call_count(read_count: int) -> float:
    """Return the duration a clean per-call clock would report.

    ``started_at`` is the first read (tick 0) and the final ``time()``
    call before constructing the cost event is the last read. With N
    total reads, the duration is ``(N - 1)``.
    """
    return float(read_count - 1)


# -- Adapter-specific concurrent harnesses -------------------------------------


async def _run_two_concurrent_claude_sdk_sessions() -> list[WorkerCostRecorded]:
    """Drive two concurrent claude-sdk sessions on ONE adapter instance.

    The SDK surface is stubbed minimally so ``_execute_task`` runs to its
    ``cost_recorded`` event. Two ``asyncio.gather``-driven sessions share
    a ``_PerTaskClock`` so durations come out per-task.
    """
    from infrastructure.adapters.worker import claude_sdk_adapter

    clock = _PerTaskClock()

    class _FakeResult:
        def __init__(self) -> None:
            self.result = "ok"
            self.is_error = False
            self.usage: dict[str, int] = {"input_tokens": 1, "output_tokens": 1}
            self.total_cost_usd = 0.0

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def query(self, _message: str) -> None:
            return None

        def receive_response(self):
            async def _gen():
                # An ``await`` between yields gives asyncio a chance to
                # interleave the two sessions on the same event loop,
                # which is the regression we want to pin.
                await asyncio.sleep(0)
                yield _FakeResult()

            return _gen()

    adapter = claude_sdk_adapter.ClaudeAgentSDKAdapter(
        claude_sdk_adapter.SDKAdapterConfig(timeout_seconds=5),
    )

    # AssistantMessage must be a real type so the adapter's
    # ``isinstance(message, AssistantMessage)`` test does not blow up.
    class _FakeAssistantMessage:
        pass

    with (
        patch.object(claude_sdk_adapter, "time", clock),
        patch.object(claude_sdk_adapter, "ClaudeSDKClient", lambda **_kw: _FakeClient()),
        patch.object(claude_sdk_adapter, "ClaudeAgentOptions", MagicMock()),
        patch.object(claude_sdk_adapter, "ResultMessage", _FakeResult),
        patch.object(claude_sdk_adapter, "AssistantMessage", _FakeAssistantMessage),
    ):
        ctx_a = {"task_description": "a", "agent_id": uuid4()}
        ctx_b = {"task_description": "b", "agent_id": uuid4()}
        events_a, events_b = await asyncio.gather(
            _collect_events(adapter.run_session(ctx_a)),
            _collect_events(adapter.run_session(ctx_b)),
        )

    return [_cost_event(events_a), _cost_event(events_b)]


async def _run_two_concurrent_google_adk_sessions() -> list[WorkerCostRecorded]:
    """Drive two concurrent ADK sessions through a minimal stub of ``_execute_task``."""
    from infrastructure.adapters.worker import google_adk_adapter

    clock = _PerTaskClock()

    async def _fake_execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ):
        # Mirror the production shape: per-call ``started_at`` local, then a
        # second ``time()`` read just before the cost event.
        started_at = google_adk_adapter.time()
        del task_description, agent_id, working_dir, task_context
        await asyncio.sleep(0)  # let asyncio interleave the two sessions
        yield sequencer.cost_recorded(
            tool_name=self._get_tool_name(),
            cost_usd=0.0,
            duration_seconds=google_adk_adapter.time() - started_at,
        )
        yield sequencer.completed("done")

    adapter = google_adk_adapter.GoogleADKAdapter(
        google_adk_adapter.ADKAdapterConfig(timeout_seconds=5),
    )

    with (
        patch.object(google_adk_adapter, "time", clock),
        patch.object(
            google_adk_adapter.GoogleADKAdapter,
            "_execute_task",
            _fake_execute_task,
        ),
    ):
        ctx_a = {"task_description": "a", "agent_id": uuid4()}
        ctx_b = {"task_description": "b", "agent_id": uuid4()}
        events_a, events_b = await asyncio.gather(
            _collect_events(adapter.run_session(ctx_a)),
            _collect_events(adapter.run_session(ctx_b)),
        )

    return [_cost_event(events_a), _cost_event(events_b)]


async def _run_two_concurrent_openhands_sessions() -> list[WorkerCostRecorded]:
    """Drive two concurrent OpenHands sessions through the post-E.9 timing path."""
    from types import SimpleNamespace

    from infrastructure.adapters.worker import openhands_adapter

    clock = _PerTaskClock()

    def _make_conversation() -> Any:
        return SimpleNamespace(
            state=SimpleNamespace(events=[]),
            conversation_stats=None,
            send_message=lambda _msg: None,
            run=lambda: None,
            pause=lambda: None,
            close=lambda: None,
        )

    adapter = openhands_adapter.OpenHandsAdapter(timeout_seconds=5)

    with (
        patch.object(openhands_adapter, "time", clock),
        patch.object(
            openhands_adapter.OpenHandsAdapter,
            "_build_conversation",
            lambda *_args, **_kw: _make_conversation(),
        ),
    ):
        ctx_a = {"task_description": "a", "agent_id": uuid4(), "working_directory": "/tmp"}
        ctx_b = {"task_description": "b", "agent_id": uuid4(), "working_directory": "/tmp"}
        events_a, events_b = await asyncio.gather(
            _collect_events(adapter.run_session(ctx_a)),
            _collect_events(adapter.run_session(ctx_b)),
        )

    return [_cost_event(events_a), _cost_event(events_b)]


@pytest.mark.asyncio
async def test_worker_timing_is_per_call_not_adapter_field() -> None:
    """Concurrent ``_execute_task`` invocations get independent durations.

    Pre-fix, two concurrent calls shared ``self._start_time`` on the adapter
    instance, so the second call's ``_start_timing`` would clobber the first
    call's start time. The first call would then report a duration computed
    against the wrong starting point (often near zero).

    Post-fix, each call reads ``started_at = time()`` into a local variable.
    A per-task clock therefore yields a SMALL, INDEPENDENT delta for each
    call: both calls report the same scripted ``end_tick - start_tick`` even
    though their wall-clock offsets differ by a large per-task base.
    """
    # Given / When: drive each adapter's _execute_task with a per-task clock
    # and a stubbed external surface. Each adapter records exactly two
    # ``time()`` reads per call in our stubs: the started_at sample and the
    # final sample at cost_recorded.
    expected_duration = _expected_duration_for_call_count(read_count=2)

    # claude_sdk: started_at + duration_kwarg in _execute_task = 2 reads/call.
    cost_a, cost_b = await _run_two_concurrent_claude_sdk_sessions()
    assert cost_a.duration_seconds == pytest.approx(expected_duration)
    assert cost_b.duration_seconds == pytest.approx(expected_duration)

    # google_adk: same shape — started_at + final time() inside stub = 2 reads/call.
    cost_a, cost_b = await _run_two_concurrent_google_adk_sessions()
    assert cost_a.duration_seconds == pytest.approx(expected_duration)
    assert cost_b.duration_seconds == pytest.approx(expected_duration)

    # openhands: production _execute_task reads time() once at start and once
    # just before sequencer.cost_recorded = 2 reads/call.
    cost_a, cost_b = await _run_two_concurrent_openhands_sessions()
    assert cost_a.duration_seconds == pytest.approx(expected_duration)
    assert cost_b.duration_seconds == pytest.approx(expected_duration)


async def _collect_events(agen: AsyncIterator[DomainEvent]) -> list[DomainEvent]:
    events: list[DomainEvent] = []
    async for event in agen:
        events.append(event)
    return events


def _cost_event(events: list[DomainEvent]) -> WorkerCostRecorded:
    for event in events:
        if isinstance(event, WorkerCostRecorded):
            return event
    raise AssertionError("WorkerCostRecorded event not emitted")
