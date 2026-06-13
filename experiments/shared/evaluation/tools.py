"""Tool-call metrics: total and breakdowns by BEF subtree, category, and node role.

A "tool call" is a ``ThoughtCaptured`` with ``output_type == "tool_use"`` and a
non-empty ``tool_name`` (agent thoughts and tool results are excluded). The
``Finish`` control signal is excluded from every *total* and from the by-subtree
and by-node breakdowns, but is surfaced in the by-category breakdown for
transparency.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from experiments.shared.evaluation.common import (
    build_bef_phase_map,
    build_role_map,
    is_counted_tool_call,
    iter_tool_calls,
    role_key,
)
from experiments.shared.evaluation.models import BefPhase, CountBreakdown, ToolCall


if TYPE_CHECKING:
    from experiments.shared.evaluation.models import RunData


def tool_calls(run_data: RunData) -> list[ToolCall]:
    """All reconstructed tool calls (including the Finish control signal)."""
    return list(iter_tool_calls(run_data.events))


def tool_call_total(run_data: RunData) -> int:
    """Total number of tool calls, excluding the Finish control signal."""
    return sum(1 for call in iter_tool_calls(run_data.events) if is_counted_tool_call(call))


def tool_calls_by_bef(run_data: RunData) -> CountBreakdown:
    """Counted tool calls grouped by BEF subtree (sums to ``tool_call_total``)."""
    phase_map = build_bef_phase_map(run_data.events, run_data.run_id)
    counts: Counter[str] = Counter()
    for call in iter_tool_calls(run_data.events):
        if not is_counted_tool_call(call):
            continue
        phase = phase_map.get(call.agent_id, BefPhase.ORCHESTRATION)
        counts[phase.value] += 1
    by = dict(counts)
    return CountBreakdown(total=sum(by.values()), by=by)


def tool_calls_by_category(run_data: RunData) -> CountBreakdown:
    """Tool calls grouped by category.

    ``by`` includes the ``Finish`` category for transparency; ``total`` excludes
    it, matching :func:`tool_call_total`.
    """
    counts: Counter[str] = Counter()
    counted_total = 0
    for call in iter_tool_calls(run_data.events):
        counts[call.category.value] += 1
        if is_counted_tool_call(call):
            counted_total += 1
    return CountBreakdown(total=counted_total, by=dict(counts))


def tool_calls_by_node(run_data: RunData) -> CountBreakdown:
    """Counted tool calls grouped by node role (sums to ``tool_call_total``)."""
    role_map = build_role_map(run_data.events)
    counts: Counter[str] = Counter()
    for call in iter_tool_calls(run_data.events):
        if not is_counted_tool_call(call):
            continue
        counts[role_key(role_map, call.agent_id)] += 1
    by = dict(counts)
    return CountBreakdown(total=sum(by.values()), by=by)
