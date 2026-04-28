"""Direct tests for ``infrastructure.workers.openhands_worker.OpenHandsWorker``.

The wrapped ``OpenHandsAdapter`` is replaced with a fake that yields a
controlled stream of domain events. We assert the bridge correctly classifies
the run from terminal events, surfaces summary text, and contains adapter
exceptions inside the WorkerPort contract.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from core.application.run_invariants import (
    TaskPromptSpec,
    TimeoutBudget,
    ToolPolicy,
    WorkerResult,
    WorkspaceSpec,
)
from core.domain.events.events import (
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from infrastructure.workers.openhands_worker import OpenHandsWorker


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _FakeOpenHandsAdapter:
    """Minimal fake satisfying the call shape ``run_session(task_context)``."""

    def __init__(self, events: list[DomainEvent], *, raise_after: int | None = None) -> None:
        self._events = events
        self._raise_after = raise_after
        self.calls: list[dict] = []

    async def run_session(self, task_context: dict) -> AsyncIterator[DomainEvent]:
        self.calls.append(task_context)
        for idx, event in enumerate(self._events):
            if self._raise_after is not None and idx == self._raise_after:
                raise RuntimeError("simulated adapter failure")
            yield event


def _make_invocation(tmp_path: Path) -> dict:
    return {
        "run_id": uuid4(),
        "spec": TaskPromptSpec(
            rendered_prompt="rendered",
            prompt_sha="0" * 64,
            cve_context=None,
            task="t",
        ),
        "tool_policy": ToolPolicy(
            allowed=("*",),
            disallowed=(),
            allowed_bash_commands=(),
        ),
        "timeouts": TimeoutBudget(per_worker_call=120, per_run_total=600),
        "workspace": WorkspaceSpec(root=tmp_path, extras={}),
    }


def test_openhands_worker_classifies_workcompleted_as_completed(tmp_path: Path) -> None:
    """A successful event stream ending in WorkCompleted yields exit_status='completed'."""
    # Given: a fake adapter producing thoughts followed by a WorkCompleted.
    fake_aggregate = uuid4()
    fake_events: list[DomainEvent] = [
        ThoughtCaptured(
            aggregate_id=fake_aggregate,
            sequence_number=2,
            content="thinking",
            stream="openhands",
            output_type="thinking",
        ),
        WorkCompleted(
            aggregate_id=fake_aggregate,
            sequence_number=3,
            result="all done",
        ),
    ]
    adapter = _FakeOpenHandsAdapter(fake_events)
    worker = OpenHandsWorker(adapter=adapter)  # type: ignore[arg-type]

    # When: invoking run_task.
    result = asyncio.run(worker.run_task(**_make_invocation(tmp_path)))

    # Then: the WorkerResult reflects success and surfaces the completion text.
    assert result.exit_status == "completed"
    assert result.output_summary == "all done"
    # And: the wrapped adapter saw a task_context derived from the spec.
    assert len(adapter.calls) == 1
    task_context = adapter.calls[0]
    assert task_context["task_description"] == "rendered"
    assert task_context["working_directory"] == str(tmp_path)
    assert task_context["tool_name"] == "openhands"


def test_openhands_worker_classifies_workfailed_as_failed(tmp_path: Path) -> None:
    """A stream ending in WorkFailed yields exit_status='failed'."""
    fake_aggregate = uuid4()
    fake_events: list[DomainEvent] = [
        WorkFailed(
            aggregate_id=fake_aggregate,
            sequence_number=2,
            reason="oom",
        ),
    ]
    adapter = _FakeOpenHandsAdapter(fake_events)
    worker = OpenHandsWorker(adapter=adapter)  # type: ignore[arg-type]

    result = asyncio.run(worker.run_task(**_make_invocation(tmp_path)))

    assert result.exit_status == "failed"
    assert result.output_summary == "oom"


def test_openhands_worker_returns_failed_when_adapter_raises(tmp_path: Path) -> None:
    """An exception from the adapter is wrapped in a WorkerResult, never propagated."""
    fake_aggregate = uuid4()
    fake_events: list[DomainEvent] = [
        ThoughtCaptured(
            aggregate_id=fake_aggregate,
            sequence_number=2,
            content="boom imminent",
            stream="openhands",
            output_type="thinking",
        ),
    ]
    adapter = _FakeOpenHandsAdapter(fake_events, raise_after=0)
    worker = OpenHandsWorker(adapter=adapter)  # type: ignore[arg-type]

    # When/Then: run_task returns a failed WorkerResult instead of propagating.
    result = asyncio.run(worker.run_task(**_make_invocation(tmp_path)))

    assert result.exit_status == "failed"
    assert isinstance(result, WorkerResult)
    assert "OpenHandsWorker error" in (result.output_summary or "")


def test_openhands_worker_defaults_to_failed_on_empty_stream(tmp_path: Path) -> None:
    """A stream with no terminal event still returns a deterministic WorkerResult."""
    adapter = _FakeOpenHandsAdapter([])
    worker = OpenHandsWorker(adapter=adapter)  # type: ignore[arg-type]

    result = asyncio.run(worker.run_task(**_make_invocation(tmp_path)))

    # Without explicit success/failure events, the bridge errs on the side of
    # 'failed' so callers don't mis-mark a stalled session as completed.
    assert result.exit_status == "failed"
