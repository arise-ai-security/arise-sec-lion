"""Synthetic ``EventRow`` factories for analysis-module unit tests.

Builders here mirror the shapes produced by the real event store so that
metric computers under ``experiments.shared.scripts.analysis`` can be
exercised without a database. The ``tool_use_event`` helper covers both
the A1/A2 buggy path (where ``tool_name`` is dropped) and the B1 SDK
path (where the structured field is populated).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from experiments.shared.scripts.db.models import EventRow


_BASE_TIME = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Generic factory
# ---------------------------------------------------------------------------


def event_row(
    aggregate_id: UUID,
    seq: int,
    event_type: str,
    payload: dict,
    *,
    offset_ms: int = 0,
) -> EventRow:
    """Build an ``EventRow`` with sensible defaults for fields not under test."""
    return EventRow(
        event_id=uuid4(),
        aggregate_id=aggregate_id,
        sequence_number=seq,
        event_type=event_type,
        payload=payload,
        occurred_at=_BASE_TIME + timedelta(milliseconds=offset_ms),
        metadata={},
    )


# ---------------------------------------------------------------------------
# Tool-use factory — produces content shaped like ``format_tool_event``.
# Supports both the A1/A2 buggy path (tool_name dropped) and the B1 SDK
# path (tool_name populated).
# ---------------------------------------------------------------------------


_TOOL_CONTENT_FORMATTERS: dict[str, Any] = {
    "Bash": lambda i: f"Running: {i.get('description') or i.get('command', '')[:100]}",
    "Read": lambda i: f"Reading: {i.get('file_path', '')}",
    "Write": lambda i: f"Writing: {i.get('file_path', '')}",
    "Edit": lambda i: f"Editing: {i.get('file_path', '')}",
    "Glob": lambda i: f"Searching files: {i.get('pattern', '')}",
    "Grep": lambda i: f"Searching content: {i.get('pattern', '')}",
}


def _format_tool_content(tool_name: str, tool_input: dict) -> str:
    formatter = _TOOL_CONTENT_FORMATTERS.get(tool_name)
    header = formatter(tool_input) if formatter is not None else f"Tool: {tool_name}"
    return f"{header}\nInput: {json.dumps(tool_input, sort_keys=True)}"


def tool_use_event(
    aggregate_id: UUID,
    seq: int,
    tool_name: str,
    tool_input: dict,
    *,
    set_structured_field: bool = False,
    offset_ms: int = 0,
) -> EventRow:
    """Build a ``ThoughtCaptured`` row mimicking a tool-use event.

    Args:
        aggregate_id: Aggregate the event is appended to.
        seq: Sequence number on that aggregate.
        tool_name: Canonical tool identifier (``Bash``, ``Read``, ...).
        tool_input: Tool-input dict serialized after the ``\\nInput: `` marker.
        set_structured_field: ``False`` reproduces the A1/A2 bug where
            ``tool_name`` is dropped and ``stream='claude_code'``. ``True``
            reproduces the B1 SDK path where ``tool_name`` is populated and
            ``stream='claude_sdk'``.
        offset_ms: Offset from ``_BASE_TIME`` for ``occurred_at``.
    """
    content = _format_tool_content(tool_name, tool_input)
    payload: dict[str, Any] = {
        "output_type": "tool_use",
        "content": content,
        "tool_name": tool_name if set_structured_field else None,
        "stream": "claude_sdk" if set_structured_field else "claude_code",
    }
    return event_row(
        aggregate_id, seq, "ThoughtCaptured", payload, offset_ms=offset_ms,
    )


# ---------------------------------------------------------------------------
# Per-event-type helpers
# ---------------------------------------------------------------------------


def agent_created(
    aggregate_id: UUID,
    seq: int,
    *,
    role: str = "boss",
    parent_id: UUID | None = None,
    sibling_index: int = 0,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "role": role,
        "parent_id": str(parent_id) if parent_id is not None else None,
        "sibling_index": sibling_index,
    }
    return event_row(aggregate_id, seq, "AgentCreated", payload, offset_ms=offset_ms)


def task_assigned(
    aggregate_id: UUID,
    seq: int,
    *,
    task_description: str = "task",
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id,
        seq,
        "TaskAssigned",
        {"task_description": task_description},
        offset_ms=offset_ms,
    )


def run_started(
    aggregate_id: UUID,
    seq: int,
    *,
    task_description: str = "task",
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id,
        seq,
        "RunStarted",
        {"task_description": task_description},
        offset_ms=offset_ms,
    )


def run_completed(
    aggregate_id: UUID,
    seq: int,
    *,
    status: str = "completed",
    duration_seconds: float = 10.0,
    total_agents: int = 1,
    completed_agents: int = 1,
    failed_agents: int = 0,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "status": status,
        "duration_seconds": duration_seconds,
        "total_agents": total_agents,
        "completed_agents": completed_agents,
        "failed_agents": failed_agents,
    }
    return event_row(aggregate_id, seq, "RunCompleted", payload, offset_ms=offset_ms)


def work_completed(
    aggregate_id: UUID,
    seq: int,
    *,
    result: str = "ok",
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id, seq, "WorkCompleted", {"result": result}, offset_ms=offset_ms,
    )


def work_failed(
    aggregate_id: UUID,
    seq: int,
    *,
    reason: str = "generic failure",
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id, seq, "WorkFailed", {"reason": reason}, offset_ms=offset_ms,
    )


def tokens_consumed(
    aggregate_id: UUID,
    seq: int,
    *,
    model: str = "claude-sonnet",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    total_tokens: int = 150,
    cost_usd: float = 0.01,
    operation: str = "worker_execution",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "operation": operation,
    }
    return event_row(aggregate_id, seq, "TokensConsumed", payload, offset_ms=offset_ms)


def worker_cost_recorded(
    aggregate_id: UUID,
    seq: int,
    *,
    tool_name: str = "claude_code",
    model: str | None = "claude-sonnet",
    cost_usd: float = 0.05,
    duration_seconds: float = 30.0,
    tokens: int = 200,
    prompt_tokens: int = 150,
    completion_tokens: int = 50,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "model": model,
        "cost_usd": cost_usd,
        "duration_seconds": duration_seconds,
        "tokens": tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
    }
    return event_row(
        aggregate_id, seq, "WorkerCostRecorded", payload, offset_ms=offset_ms,
    )


def agent_execution_started(
    aggregate_id: UUID,
    seq: int,
    *,
    role: str = "worker",
    depth: int = 0,
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id,
        seq,
        "AgentExecutionStarted",
        {"role": role, "depth": depth},
        offset_ms=offset_ms,
    )


def agent_execution_finished(
    aggregate_id: UUID,
    seq: int,
    *,
    role: str = "worker",
    status: str = "completed",
    duration_seconds: float = 10.0,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "role": role,
        "status": status,
        "duration_seconds": duration_seconds,
    }
    return event_row(
        aggregate_id, seq, "AgentExecutionFinished", payload, offset_ms=offset_ms,
    )


def child_spawned(
    aggregate_id: UUID,
    seq: int,
    *,
    child_id: UUID,
    child_role: str = "worker",
    sibling_index: int = 0,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "child_id": str(child_id),
        "child_role": child_role,
        "sibling_index": sibling_index,
    }
    return event_row(aggregate_id, seq, "ChildSpawned", payload, offset_ms=offset_ms)


def child_completed(
    parent_id: UUID,
    seq: int,
    *,
    child_id: UUID,
    result: str = "ok",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {"child_id": str(child_id), "result": result}
    return event_row(parent_id, seq, "ChildCompleted", payload, offset_ms=offset_ms)


def child_failed(
    parent_id: UUID,
    seq: int,
    *,
    child_id: UUID,
    reason: str = "failed",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {"child_id": str(child_id), "reason": reason}
    return event_row(parent_id, seq, "ChildFailed", payload, offset_ms=offset_ms)


def verification_passed(
    aggregate_id: UUID,
    seq: int,
    *,
    score: int = 100,
    feedback: str = "",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {"score": score, "feedback": feedback}
    return event_row(
        aggregate_id, seq, "VerificationPassed", payload, offset_ms=offset_ms,
    )


def verification_failed(
    aggregate_id: UUID,
    seq: int,
    *,
    failed_stage: str = "judge",
    score: int = 0,
    feedback: str = "",
    stages_passed: tuple[str, ...] = (),
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "failed_stage": failed_stage,
        "score": score,
        "feedback": feedback,
        "stages_passed": list(stages_passed),
    }
    return event_row(
        aggregate_id, seq, "VerificationFailed", payload, offset_ms=offset_ms,
    )


def retry_scheduled(
    aggregate_id: UUID,
    seq: int,
    *,
    attempt: int = 2,
    reason: str = "retry",
    escalated_model: str = "",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "attempt": attempt,
        "reason": reason,
        "escalated_model": escalated_model,
    }
    return event_row(aggregate_id, seq, "RetryScheduled", payload, offset_ms=offset_ms)


def redecomposition_triggered(
    aggregate_id: UUID,
    seq: int,
    *,
    trigger_child_id: UUID,
    reason: str = "redecompose",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "trigger_child_id": str(trigger_child_id),
        "reason": reason,
    }
    return event_row(
        aggregate_id, seq, "RedecompositionTriggered", payload, offset_ms=offset_ms,
    )


def decision_infeasible(
    aggregate_id: UUID,
    seq: int,
    *,
    reason: str = "infeasible",
    minimum_subtasks: int = 1,
    minimum_depth: int = 1,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "reason": reason,
        "minimum_subtasks": minimum_subtasks,
        "minimum_depth": minimum_depth,
    }
    return event_row(
        aggregate_id, seq, "DecisionInfeasible", payload, offset_ms=offset_ms,
    )


def limit_enforced(
    aggregate_id: UUID,
    seq: int,
    *,
    limit_type: str = "depth",
    limit_value: int = 3,
    attempted_value: int = 4,
    action_taken: str = "rejected_children",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "limit_type": limit_type,
        "limit_value": limit_value,
        "attempted_value": attempted_value,
        "action_taken": action_taken,
    }
    return event_row(aggregate_id, seq, "LimitEnforced", payload, offset_ms=offset_ms)


def operation_started(
    aggregate_id: UUID,
    seq: int,
    *,
    operation_type: str = "worker_execution",
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id,
        seq,
        "OperationStarted",
        {"operation_type": operation_type},
        offset_ms=offset_ms,
    )


def operation_finished(
    aggregate_id: UUID,
    seq: int,
    *,
    operation_type: str = "worker_execution",
    duration_seconds: float = 5.0,
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "operation_type": operation_type,
        "duration_seconds": duration_seconds,
    }
    return event_row(
        aggregate_id, seq, "OperationFinished", payload, offset_ms=offset_ms,
    )


def probe_started(
    aggregate_id: UUID,
    seq: int,
    *,
    probe_type: str = "file_check",
    offset_ms: int = 0,
) -> EventRow:
    return event_row(
        aggregate_id,
        seq,
        "ProbeStarted",
        {"probe_type": probe_type},
        offset_ms=offset_ms,
    )


def probe_completed(
    aggregate_id: UUID,
    seq: int,
    *,
    probe_type: str = "file_check",
    result_summary: str = "ok",
    offset_ms: int = 0,
) -> EventRow:
    payload: dict[str, Any] = {
        "probe_type": probe_type,
        "result_summary": result_summary,
    }
    return event_row(
        aggregate_id, seq, "ProbeCompleted", payload, offset_ms=offset_ms,
    )
