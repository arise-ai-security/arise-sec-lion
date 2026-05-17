"""Pure transforms over `EventRow` lists.

No DB, no asyncpg. Three responsibilities:
  - `build_agent_trajectory`: glue own events + parent outcomes
  - `build_parent_trace`:     glue events from each ancestor agent
  - `prettify_trajectory`:    render either as human-readable lines

The split from `event_queries.py` is purely about testability — these
functions can be unit-tested with synthetic in-memory rows without an
asyncpg/Postgres install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import orjson


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from experiments.shared.scripts.db.models import EventRow


# =============================================================================
# build_agent_trajectory  —  one agent's events + parent's outcome
# =============================================================================
#
#     BOSS                                  ← not included
#     ├── Mgr-1  ──┐                        ← only its ChildCompleted/
#     │   ├── Worker-A  ◀── target              ChildFailed for Worker-A
#     │   └── Worker-B                      ← not included
#     └── Mgr-2                             ← not included
#
# Own events first (sequence-ordered), then parent outcomes (time-ordered)
# appended at the tail. Outcomes are forced to the tail rather than
# merge-sorted by `occurred_at` so a microsecond collision between
# `WorkCompleted` (child) and `ChildCompleted` (parent) cannot reorder
# the causally-impossible "parent reports before child finishes" pair.


def build_agent_trajectory(
    own_events: Sequence[EventRow],
    parent_outcomes: Iterable[EventRow] = (),
) -> list[EventRow]:
    sorted_own = sorted(own_events, key=lambda e: e.sequence_number)
    sorted_outcomes = sorted(
        parent_outcomes, key=lambda e: (e.occurred_at, e.sequence_number)
    )
    return [*sorted_own, *sorted_outcomes]


# =============================================================================
# build_parent_trace  —  every ancestor's events in chronological order
# =============================================================================
#
#     BOSS              ◀── included
#     ├── Mgr-1         ◀── included
#     │   ├── Worker-A  ◀── target (included)
#     │   └── Worker-B  ← not included
#     └── Mgr-2         ← not included
#
# Given a multi-aggregate row set already restricted to the ancestor
# chain by the SQL layer, this just enforces stable ordering: globally
# by `occurred_at`, with `sequence_number` and `aggregate_id` as
# tiebreakers so events emitted on the same microsecond by different
# ancestors land in a deterministic order.
#
# Caveat: `sequence_number` is unique only WITHIN an aggregate, so on a
# tied `occurred_at` across two aggregates the tertiary `aggregate_id`
# string is what produces determinism — not causality. Parent-before-
# child semantic ordering on microsecond ties relies on the wall clock
# having been monotonic at emission time, which is the system contract.


def build_parent_trace(events: Sequence[EventRow]) -> list[EventRow]:
    return sorted(
        events,
        key=lambda e: (e.occurred_at, e.sequence_number, str(e.aggregate_id)),
    )


# =============================================================================
# prettify_trajectory  —  human-readable rendering
# =============================================================================
#
# `ThoughtCaptured` is ~94% of all rows in the corpus. Naïve printing is
# unusable, so consecutive `ThoughtCaptured` events are folded into one
# summary line:
#     [seq N..M] ThoughtCaptured xK (tools: foo, bar)
#
# Aggregate boundary forces a flush — collapsing `ThoughtCaptured` rows
# that belong to DIFFERENT agents (boss vs worker vs ...) would lose
# per-agent attribution, which matters for `build_parent_trace` output.


_PRETTY_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "AgentCreated": ("role", "parent_id", "sibling_index"),
    "TaskAssigned": ("task_description",),
    "StatusChanged": ("old_status", "new_status", "reason"),
    "ComplexityEvaluated": ("complexity", "determined_role"),
    "SubtasksDefined": ("subtasks",),
    "ChildSpawned": ("child_id", "child_role", "sibling_index"),
    "ChildCompleted": ("child_id", "result"),
    "ChildFailed": ("child_id", "reason"),
    "WorkCompleted": ("result",),
    "WorkFailed": ("reason",),
    "VerificationFailed": ("failed_stage", "score"),
    "VerificationPassed": ("score",),
    "DecisionInfeasible": ("reason",),
    "RedecompositionTriggered": ("trigger_child_id", "reason"),
    "RetryScheduled": ("attempt", "reason"),
    "ProbeStarted": ("probe_type",),
    "ProbeCompleted": ("probe_type",),
    "CodeGenerationStarted": ("tool_name",),
    "PromptSent": ("prompt_type", "target"),
    "OperationStarted": ("operation_type",),
    "OperationFinished": ("operation_type", "duration_seconds"),
    "AgentExecutionStarted": ("role", "depth"),
    "AgentExecutionFinished": ("role", "status", "duration_seconds"),
    "RunStarted": ("task_description",),
    "RunCompleted": ("status", "duration_seconds", "total_agents",
                     "completed_agents", "failed_agents"),
    "TokensConsumed": ("model", "total_tokens", "cost_usd", "operation"),
    "WorkerCostRecorded": ("tool_name", "model", "cost_usd", "duration_seconds"),
    "LimitEnforced": ("limit_type", "limit_value", "attempted_value",
                      "action_taken"),
}


def _render_payload(event_type: str, payload: dict) -> str:
    keys = _PRETTY_KEY_FIELDS.get(event_type)
    if keys:
        parts: list[str] = []
        for k in keys:
            v = payload.get(k)
            if v is None:
                continue
            if isinstance(v, list):
                parts.append(f"{k}=[{len(v)} items]")
            elif isinstance(v, str) and len(v) > 80:
                parts.append(f'{k}="{v[:77]}..."')
            else:
                parts.append(f"{k}={v!r}")
        return " ".join(parts)
    snippet = orjson.dumps(payload).decode("utf-8")
    return snippet if len(snippet) <= 120 else snippet[:117] + "..."


def _format_event(e: EventRow) -> str:
    ts = e.occurred_at.strftime("%H:%M:%S.%f")[:-3]
    return (
        f"  [seq={e.sequence_number:>4} @ {ts}] {e.event_type:30s} "
        f"{_render_payload(e.event_type, e.payload)}"
    )


def _header(trajectory: Sequence[EventRow]) -> str:
    first = trajectory[0]
    aggregates = {e.aggregate_id for e in trajectory}
    role = next(
        (e.payload.get("role", "") for e in trajectory
         if e.event_type == "AgentCreated" and e.aggregate_id == first.aggregate_id),
        "",
    )
    parts = [
        f"events={len(trajectory)}",
        f"aggregates={len(aggregates)}" if len(aggregates) > 1 else f"agent={first.aggregate_id}",
    ]
    if role:
        parts.insert(0, f"role={role}")
    return "trajectory  " + "  ".join(parts)


def prettify_trajectory(
    trajectory: Sequence[EventRow], *, collapse_thoughts: bool = True
) -> list[str]:
    if not trajectory:
        return []

    out: list[str] = [_header(trajectory)]
    buffer: list[EventRow] = []

    def flush() -> None:
        if not buffer:
            return
        if len(buffer) == 1:
            out.append(_format_event(buffer[0]))
        else:
            tools = sorted({
                e.payload.get("tool_name") or ""
                for e in buffer
                if e.payload.get("tool_name")
            })
            ts = buffer[0].occurred_at.strftime("%H:%M:%S.%f")[:-3]
            tools_part = f" (tools: {', '.join(tools)})" if tools else ""
            out.append(
                f"  [seq={buffer[0].sequence_number:>4}..{buffer[-1].sequence_number:<4} "
                f"@ {ts}] ThoughtCaptured x{len(buffer)}{tools_part}"
            )
        buffer.clear()

    for e in trajectory:
        same_aggregate = buffer and buffer[-1].aggregate_id == e.aggregate_id
        is_thought = e.event_type == "ThoughtCaptured"
        if collapse_thoughts and is_thought and (not buffer or same_aggregate):
            buffer.append(e)
        else:
            flush()
            if collapse_thoughts and e.event_type == "ThoughtCaptured":
                buffer.append(e)
            else:
                out.append(_format_event(e))
    flush()
    return out
