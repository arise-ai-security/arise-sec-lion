"""Cost-metric computer over a run's event stream (design doc §5)."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.models import CostMetrics


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


_UNKNOWN = "unknown"


def compute_cost(events: Sequence[EventRow]) -> CostMetrics:
    """Aggregate cost metrics from a run's event stream.

    Token-source rule (see design doc §5 and the resolution in the cost
    spec): per-event-type accumulation, no cross-source summing.
    - ``prompt_tokens``, ``completion_tokens``, ``total_tokens`` come from
      ``TokensConsumed`` only.
    - ``cache_read_tokens``, ``cache_write_tokens``, ``reasoning_tokens``
      come from ``WorkerCostRecorded`` only (``TokensConsumed`` payloads
      do not carry cache or reasoning token fields).
    - ``total_llm_cost_usd`` sums ``TokensConsumed.cost_usd``;
      ``total_worker_cost_usd`` sums ``WorkerCostRecorded.cost_usd``.
    - ``cost_by_model`` and ``cost_by_operation`` only group
      ``TokensConsumed`` cost (``WorkerCostRecorded`` carries neither
      field).
    """
    total_llm_cost_usd = 0.0
    total_worker_cost_usd = 0.0
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0
    llm_call_count = 0
    cost_by_model: dict[str, float] = defaultdict(float)
    cost_by_operation: dict[str, float] = defaultdict(float)

    for event in events:
        payload = event.payload
        if event.event_type == "TokensConsumed":
            cost = float(payload.get("cost_usd") or 0.0)
            total_llm_cost_usd += cost
            prompt_tokens += int(payload.get("prompt_tokens") or 0)
            completion_tokens += int(payload.get("completion_tokens") or 0)
            total_tokens += int(payload.get("total_tokens") or 0)
            llm_call_count += 1
            model = payload.get("model") or _UNKNOWN
            operation = payload.get("operation") or _UNKNOWN
            cost_by_model[model] += cost
            cost_by_operation[operation] += cost
        elif event.event_type == "WorkerCostRecorded":
            total_worker_cost_usd += float(payload.get("cost_usd") or 0.0)
            cache_read_tokens += int(payload.get("cache_read_tokens") or 0)
            cache_write_tokens += int(payload.get("cache_write_tokens") or 0)
            reasoning_tokens += int(payload.get("reasoning_tokens") or 0)

    return CostMetrics(
        total_llm_cost_usd=total_llm_cost_usd,
        total_worker_cost_usd=total_worker_cost_usd,
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        llm_call_count=llm_call_count,
        cost_by_model=dict(cost_by_model),
        cost_by_operation=dict(cost_by_operation),
    )
