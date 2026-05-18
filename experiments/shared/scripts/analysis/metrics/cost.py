"""Cost-metric computer over a run's event stream (design doc §5)."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.models import CostMetrics


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


_UNKNOWN = "unknown"
_WORKER_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _float(payload: dict, key: str) -> float:
    return float(payload.get(key) or 0.0)


def _int(payload: dict, key: str) -> int:
    return int(payload.get(key) or 0)


def _worker_recorded_tokens(payload: dict) -> int:
    breakdown_total = sum(_int(payload, key) for key in _WORKER_TOKEN_FIELDS)
    if breakdown_total:
        return breakdown_total
    return _int(payload, "tokens")


def _worker_model_costs(payload: dict, event_cost: float) -> dict[str, float]:
    costs: dict[str, float] = defaultdict(float)
    usage_metrics = payload.get("usage_metrics")
    if isinstance(usage_metrics, list):
        for usage in usage_metrics:
            if not isinstance(usage, dict):
                continue
            model = usage.get("model")
            if not model:
                continue
            model_cost = float(usage.get("accumulated_cost_usd") or 0.0)
            if model_cost:
                costs[str(model)] += model_cost
    if costs:
        return dict(costs)

    model = payload.get("model") or _UNKNOWN
    return {str(model): event_cost} if event_cost else {}


def compute_cost(events: Sequence[EventRow]) -> CostMetrics:
    """Aggregate cost metrics from a run's event stream.

    ``TokensConsumed`` is the manager/orchestration channel. It covers BOSS,
    MANAGER/PENDING, and judge LLM calls. ``WorkerCostRecorded`` is the worker
    execution channel. The total cost is the sum of both channels, and model
    attribution keeps both channel-specific and combined views.
    """
    manager_cost_usd = 0.0
    worker_cost_usd = 0.0
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0
    llm_call_count = 0
    manager_cost_by_model: dict[str, float] = defaultdict(float)
    worker_cost_by_model: dict[str, float] = defaultdict(float)
    cost_by_model: dict[str, float] = defaultdict(float)
    cost_by_operation: dict[str, float] = defaultdict(float)

    for event in events:
        payload = event.payload
        if event.event_type == "TokensConsumed":
            cost = _float(payload, "cost_usd")
            manager_cost_usd += cost
            prompt_tokens += _int(payload, "prompt_tokens")
            completion_tokens += _int(payload, "completion_tokens")
            total_tokens += _int(payload, "total_tokens")
            llm_call_count += 1
            model = str(payload.get("model") or _UNKNOWN)
            operation = str(payload.get("operation") or _UNKNOWN)
            manager_cost_by_model[model] += cost
            cost_by_model[model] += cost
            cost_by_operation[operation] += cost
        elif event.event_type == "WorkerCostRecorded":
            cost = _float(payload, "cost_usd")
            worker_cost_usd += cost
            prompt_tokens += _int(payload, "prompt_tokens")
            completion_tokens += _int(payload, "completion_tokens")
            total_tokens += _worker_recorded_tokens(payload)
            cache_read_tokens += _int(payload, "cache_read_tokens")
            cache_write_tokens += _int(payload, "cache_write_tokens")
            reasoning_tokens += _int(payload, "reasoning_tokens")
            for model, model_cost in _worker_model_costs(payload, cost).items():
                worker_cost_by_model[model] += model_cost
                cost_by_model[model] += model_cost

    return CostMetrics(
        manager_cost_usd=manager_cost_usd,
        worker_cost_usd=worker_cost_usd,
        total_cost_usd=manager_cost_usd + worker_cost_usd,
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        llm_call_count=llm_call_count,
        manager_cost_by_model=dict(manager_cost_by_model),
        worker_cost_by_model=dict(worker_cost_by_model),
        cost_by_model=dict(cost_by_model),
        cost_by_operation=dict(cost_by_operation),
    )
