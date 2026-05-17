#!/usr/bin/env python3
"""Show the full, prettified event stream for a single worker (aggregate).

Walks the worker's event log in sequence order and prints a chronological
narrative of what happened: tasks assigned, prompts sent, thoughts captured,
costs incurred, completions, retries, verifications, etc. Useful for
post-mortem analysis of a single worker's execution.

Usage:
    POSTGRES_PASSWORD=arise python scripts/show_worker_events.py <worker_id>
    POSTGRES_PASSWORD=arise python scripts/show_worker_events.py <worker_id> \\
        --max-thought-chars 4000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from functools import singledispatch
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID


_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.settings import ApiSettings  # noqa: E402
from core.domain.events.events import (  # noqa: E402
    AgentCreated,
    AgentExecutionFinished,
    AgentExecutionStarted,
    ArtifactStored,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DecisionInfeasible,
    DecisionRecorded,
    DomainEvent,
    LimitEnforced,
    OperationFinished,
    OperationStarted,
    ProbeCompleted,
    ProbeStarted,
    PromptSent,
    RedecompositionTriggered,
    RetryScheduled,
    RunCompleted,
    RunStarted,
    SharedContextCreated,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    VerificationFailed,
    VerificationPassed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from infrastructure.adapters.postgres_event_store import PostgresEventStore  # noqa: E402


if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


INDENT = "    "


class RenderOptions:
    """Truncation knobs for verbose payloads."""

    def __init__(
        self,
        *,
        max_thought_chars: int,
        max_prompt_chars: int,
        max_block_chars: int,
    ) -> None:
        self.max_thought_chars = max_thought_chars
        self.max_prompt_chars = max_prompt_chars
        self.max_block_chars = max_block_chars


def _short(uid: UUID | str | None) -> str:
    if uid is None:
        return "-"
    return str(uid)[:8]


def _block(value: str, *, limit: int | None = None) -> str:
    if not value:
        return ""
    text = value
    if limit is not None and len(text) > limit:
        text = text[:limit] + f"\n... (truncated, {len(value) - limit} more chars)"
    return textwrap.indent(text, INDENT)


def _kv_line(pairs: Iterable[tuple[str, Any]]) -> str:
    parts = [f"{k}={v}" for k, v in pairs if v not in (None, "", [])]
    return INDENT + "  ".join(parts) if parts else ""


_BASE_FIELDS = frozenset(
    {"event_id", "aggregate_id", "sequence_number", "occurred_at", "metadata"}
)


@singledispatch
def render_event(event: DomainEvent, _opts: RenderOptions) -> list[str]:
    payload = event.model_dump(mode="json", exclude=_BASE_FIELDS)
    body = ", ".join(f"{k}={v!r}" for k, v in payload.items())
    return [INDENT + body] if body else []


@render_event.register
def _(event: AgentCreated, _: RenderOptions) -> list[str]:
    lines = [
        _kv_line(
            [
                ("role", event.role),
                ("parent", _short(event.parent_id) if event.parent_id else "ROOT"),
                ("sibling_index", event.sibling_index),
                ("depends_on", event.depends_on or None),
                ("estimated_complexity", event.estimated_complexity),
            ]
        ),
    ]
    if event.success_criteria:
        lines.append(_kv_line([("success_criteria", event.success_criteria)]))
    if event.target_paths:
        lines.append(_kv_line([("target_paths", event.target_paths)]))
    if event.symbols:
        lines.append(_kv_line([("symbols", event.symbols)]))
    return lines


@render_event.register
def _(event: TaskAssigned, opts: RenderOptions) -> list[str]:
    return ["", _block(event.task_description, limit=opts.max_block_chars)]


@render_event.register
def _(event: StatusChanged, _: RenderOptions) -> list[str]:
    extra = f"  ({event.reason})" if event.reason else ""
    return [INDENT + f"{event.old_status} -> {event.new_status}{extra}"]


@render_event.register
def _(event: ComplexityEvaluated, _: RenderOptions) -> list[str]:
    lines = [
        _kv_line(
            [
                ("complexity", event.complexity),
                ("determined_role", event.determined_role),
            ]
        ),
    ]
    if event.reasoning:
        lines.extend(["", _block(event.reasoning)])
    return lines


@render_event.register
def _(event: SubtasksDefined, opts: RenderOptions) -> list[str]:
    lines = [INDENT + f"{len(event.subtasks)} subtask(s) defined:"]
    for i, st in enumerate(event.subtasks):
        head = f"  [{i}] {st.description}"
        if len(head) > opts.max_block_chars:
            head = head[: opts.max_block_chars] + "..."
        lines.append(INDENT + head)
        if st.depends_on:
            lines.append(INDENT + f"      depends_on={st.depends_on}")
    return lines


@render_event.register
def _(event: ChildSpawned, opts: RenderOptions) -> list[str]:
    desc = event.subtask.description
    if len(desc) > opts.max_block_chars:
        desc = desc[: opts.max_block_chars] + "..."
    return [
        _kv_line(
            [
                ("child", _short(event.child_id)),
                ("role", event.child_role),
                ("sibling_index", event.sibling_index),
            ]
        ),
        INDENT + f"task: {desc}",
    ]


@render_event.register
def _(event: ChildCompleted, _: RenderOptions) -> list[str]:
    return [INDENT + f"child {_short(event.child_id)} -> {event.result[:200]}"]


@render_event.register
def _(event: ChildFailed, _: RenderOptions) -> list[str]:
    return [INDENT + f"child {_short(event.child_id)} FAILED: {event.reason[:200]}"]


@render_event.register
def _(event: PromptSent, opts: RenderOptions) -> list[str]:
    head = _kv_line(
        [
            ("prompt_type", event.prompt_type),
            ("target", event.target),
            ("chars", len(event.prompt)),
        ]
    )
    return [head, "", _block(event.prompt, limit=opts.max_prompt_chars)]


@render_event.register
def _(event: CodeGenerationStarted, _: RenderOptions) -> list[str]:
    return [INDENT + f"tool: {event.tool_name}"]


@render_event.register
def _(event: ThoughtCaptured, opts: RenderOptions) -> list[str]:
    head = _kv_line(
        [
            ("stream", event.stream),
            ("output_type", event.output_type),
            ("tool_name", event.tool_name),
            ("chars", len(event.content)),
        ]
    )
    return [head, "", _block(event.content, limit=opts.max_thought_chars)]


@render_event.register
def _(event: WorkCompleted, opts: RenderOptions) -> list[str]:
    return ["", _block(event.result, limit=opts.max_block_chars)]


@render_event.register
def _(event: WorkFailed, opts: RenderOptions) -> list[str]:
    return ["", _block(event.reason, limit=opts.max_block_chars)]


@render_event.register
def _(event: VerificationFailed, opts: RenderOptions) -> list[str]:
    head = _kv_line(
        [
            ("failed_stage", event.failed_stage),
            ("stages_passed", event.stages_passed or None),
            ("score", event.score or None),
        ]
    )
    return [head, "", _block(event.feedback, limit=opts.max_block_chars)]


@render_event.register
def _(event: VerificationPassed, opts: RenderOptions) -> list[str]:
    head = _kv_line([("score", event.score)])
    out = [head]
    if event.feedback:
        out.extend(["", _block(event.feedback, limit=opts.max_block_chars)])
    return out


@render_event.register
def _(event: RetryScheduled, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("attempt", event.attempt),
                ("escalated_model", event.escalated_model),
            ]
        ),
        INDENT + f"reason: {event.reason[:200]}",
    ]


@render_event.register
def _(event: DecisionInfeasible, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("minimum_subtasks", event.minimum_subtasks),
                ("minimum_depth", event.minimum_depth),
            ]
        ),
        INDENT + f"reason: {event.reason[:200]}",
    ]


@render_event.register
def _(event: RedecompositionTriggered, _: RenderOptions) -> list[str]:
    return [
        INDENT + f"trigger_child={_short(event.trigger_child_id)}",
        INDENT + f"reason: {event.reason[:200]}",
    ]


@render_event.register
def _(event: ProbeStarted, _: RenderOptions) -> list[str]:
    return [INDENT + f"probe_type: {event.probe_type}"]


@render_event.register
def _(event: ProbeCompleted, _: RenderOptions) -> list[str]:
    return [INDENT + f"probe_type: {event.probe_type}  result: {event.result_summary[:200]}"]


@render_event.register
def _(event: TokensConsumed, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("model", event.model),
                ("operation", event.operation),
                ("prompt", event.prompt_tokens),
                ("completion", event.completion_tokens),
                ("total", event.total_tokens),
                ("cost_usd", f"{event.cost_usd:.6f}"),
            ]
        )
    ]


@render_event.register
def _(event: WorkerCostRecorded, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("tool_name", event.tool_name),
                ("model", event.model),
                ("tokens", event.tokens),
                ("cost_usd", f"{event.cost_usd:.6f}"),
                ("duration_s", f"{event.duration_seconds:.2f}"),
            ]
        )
    ]


@render_event.register
def _(event: LimitEnforced, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("limit_type", event.limit_type),
                ("limit_value", event.limit_value),
                ("attempted_value", event.attempted_value),
                ("action_taken", event.action_taken),
            ]
        )
    ]


@render_event.register
def _(event: AgentExecutionStarted, _: RenderOptions) -> list[str]:
    return [_kv_line([("role", event.role), ("depth", event.depth)])]


@render_event.register
def _(event: AgentExecutionFinished, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("role", event.role),
                ("status", event.status),
                ("duration_s", f"{event.duration_seconds:.2f}"),
            ]
        )
    ]


@render_event.register
def _(event: OperationStarted, _: RenderOptions) -> list[str]:
    return [INDENT + event.operation_type]


@render_event.register
def _(event: OperationFinished, _: RenderOptions) -> list[str]:
    return [INDENT + f"{event.operation_type}  ({event.duration_seconds:.2f}s)"]


@render_event.register
def _(event: RunStarted, opts: RenderOptions) -> list[str]:
    out = [INDENT + f"task_description ({len(event.task_description)} chars):"]
    out.extend(["", _block(event.task_description, limit=opts.max_block_chars)])
    if event.domain_metadata:
        out.append(INDENT + f"domain_metadata: {event.domain_metadata}")
    return out


@render_event.register
def _(event: RunCompleted, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("status", event.status),
                ("duration_s", f"{event.duration_seconds:.2f}"),
                ("total_agents", event.total_agents),
                ("completed_agents", event.completed_agents),
                ("failed_agents", event.failed_agents),
            ]
        )
    ]


@render_event.register
def _(event: SharedContextCreated, _: RenderOptions) -> list[str]:
    return [INDENT + f"root_id={_short(event.root_id)}"]


@render_event.register
def _(event: ArtifactStored, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("key", event.key),
                ("content_type", event.content_type),
                ("stored_by", _short(event.stored_by)),
                ("size", len(event.content) if event.content else "hash"),
            ]
        )
    ]


@render_event.register
def _(event: DecisionRecorded, _: RenderOptions) -> list[str]:
    return [
        _kv_line(
            [
                ("decision_key", event.decision_key),
                ("decided_by", _short(event.decided_by)),
            ]
        ),
        INDENT + f"value: {event.decision_value[:200]}",
        INDENT + f"rationale: {event.rationale[:200]}",
    ]


def _format_header(worker_id: UUID, events: list[DomainEvent]) -> str:
    created = next((e for e in events if isinstance(e, AgentCreated)), None)
    assigned = next((e for e in events if isinstance(e, TaskAssigned)), None)
    role = created.role if created else "?"
    parent = _short(created.parent_id) if created and created.parent_id else "ROOT"
    task = assigned.task_description if assigned else "(no task assigned)"

    bar = "=" * 80
    return "\n".join(
        [
            bar,
            f"Worker:        {worker_id}",
            f"Role:          {role}    Parent: {parent}",
            f"Task:          {task.splitlines()[0][:200] if task else '-'}",
            f"Total events:  {len(events)}",
            bar,
        ]
    )


def _format_footer(events: list[DomainEvent]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        name = type(event).__name__
        counts[name] = counts.get(name, 0) + 1

    total_tokens = sum(
        e.total_tokens for e in events if isinstance(e, TokensConsumed)
    )
    llm_cost = sum(e.cost_usd for e in events if isinstance(e, TokensConsumed))
    worker_cost = sum(
        e.cost_usd for e in events if isinstance(e, WorkerCostRecorded)
    )
    worker_duration = sum(
        e.duration_seconds for e in events if isinstance(e, WorkerCostRecorded)
    )

    timestamps = [e.occurred_at for e in events]
    wall_time = ""
    if len(timestamps) >= 2:
        delta = max(timestamps) - min(timestamps)
        wall_time = f"  wall_time={delta.total_seconds():.2f}s"

    bar = "-" * 80
    lines = [
        bar,
        "Summary",
        bar,
        f"  events: {sum(counts.values())}{wall_time}",
        f"  llm_tokens: {total_tokens}  llm_cost_usd: {llm_cost:.6f}",
        f"  worker_cost_usd: {worker_cost:.6f}  worker_duration_s: {worker_duration:.2f}",
        "  event_counts:",
    ]
    for name in sorted(counts):
        lines.append(f"    {name}: {counts[name]}")
    return "\n".join(lines)


def _format_event(event: DomainEvent, opts: RenderOptions) -> str:
    seq = f"#{event.sequence_number:04d}"
    ts = _iso(event.occurred_at)
    head = f"{seq}  {ts}  {type(event).__name__}"
    body_lines = [line for line in render_event(event, opts) if line]
    if not body_lines:
        return head
    return head + "\n" + "\n".join(body_lines)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


async def _run(worker_id: UUID, opts: RenderOptions) -> int:
    settings = ApiSettings.load()
    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        events = await store.get_events(worker_id)
    finally:
        await store.disconnect()

    if not events:
        print(f"(no events found for worker {worker_id})")
        return 1

    print(_format_header(worker_id, events))
    print()
    for event in events:
        print(_format_event(event, opts))
        print()
    print(_format_footer(events))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("worker_id", help="UUID of the worker (agent aggregate_id)")
    parser.add_argument(
        "--max-thought-chars",
        type=int,
        default=8000,
        help="Truncate ThoughtCaptured content longer than this (default: 8000)",
    )
    parser.add_argument(
        "--max-prompt-chars",
        type=int,
        default=8000,
        help="Truncate PromptSent content longer than this (default: 8000)",
    )
    parser.add_argument(
        "--max-block-chars",
        type=int,
        default=4000,
        help="Truncate other long text blocks (results, feedback) longer than this (default: 4000)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        worker_id = UUID(args.worker_id)
    except ValueError:
        print(f"error: invalid UUID for worker_id: {args.worker_id!r}", file=sys.stderr)
        return 2

    opts = RenderOptions(
        max_thought_chars=args.max_thought_chars,
        max_prompt_chars=args.max_prompt_chars,
        max_block_chars=args.max_block_chars,
    )
    return asyncio.run(_run(worker_id, opts))


if __name__ == "__main__":
    sys.exit(main())
