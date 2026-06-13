"""Cache-hit-rate metrics, grouped by BEF subtree or by node role.

Cache-hit rate = ``Σ cache_read_tokens / Σ prompt_tokens`` over both
``TokensConsumed`` (LLM) and ``WorkerCostRecorded`` (worker) events; ``cache_read``
is a subset of ``prompt_tokens`` in the data. A group with zero prompt tokens has
an undefined rate (``None``).
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from core.domain.events.events import TokensConsumed, WorkerCostRecorded
from experiments.shared.evaluation.common import (
    build_bef_phase_map,
    build_role_map,
    cache_rate,
    events_of_type,
    role_key,
)
from experiments.shared.evaluation.models import BefPhase, RateBreakdown


if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from experiments.shared.evaluation.models import RunData


def _read_prompt_pairs(run_data: RunData) -> Iterator[tuple[UUID, int, int]]:
    """Yield ``(agent_id, cache_read_tokens, prompt_tokens)`` per cost event."""
    for event in events_of_type(run_data.events, TokensConsumed):
        yield event.aggregate_id, event.cache_read_tokens, event.prompt_tokens
    for event in events_of_type(run_data.events, WorkerCostRecorded):
        yield event.aggregate_id, (event.cache_read_tokens or 0), (event.prompt_tokens or 0)


def _rate_breakdown(grouped: dict[str, list[int]]) -> RateBreakdown:
    """Build a RateBreakdown from ``{key: [read_sum, prompt_sum]}``."""
    by = {key: cache_rate(read, prompt) for key, (read, prompt) in grouped.items()}
    total_read = sum(read for read, _ in grouped.values())
    total_prompt = sum(prompt for _, prompt in grouped.values())
    return RateBreakdown(overall=cache_rate(total_read, total_prompt), by=by)


def cache_rate_by_bef(run_data: RunData) -> RateBreakdown:
    """Cache-hit rate per BEF subtree."""
    phase_map = build_bef_phase_map(run_data.events, run_data.run_id)
    grouped: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for agent_id, read, prompt in _read_prompt_pairs(run_data):
        key = phase_map.get(agent_id, BefPhase.ORCHESTRATION).value
        grouped[key][0] += read
        grouped[key][1] += prompt
    return _rate_breakdown(grouped)


def cache_rate_by_node(run_data: RunData) -> RateBreakdown:
    """Cache-hit rate per node role."""
    role_map = build_role_map(run_data.events)
    grouped: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for agent_id, read, prompt in _read_prompt_pairs(run_data):
        key = role_key(role_map, agent_id)
        grouped[key][0] += read
        grouped[key][1] += prompt
    return _rate_breakdown(grouped)
