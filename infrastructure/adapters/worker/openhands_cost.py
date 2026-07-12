"""OpenHands cost and usage extraction.

Normalizes the OpenHands SDK ``conversation_stats`` into a
``WorkerCostRecorded`` event. Reuses the shared usage helpers
(``UsageBreakdown`` / ``emit_cost``); the caller supplies the tool name,
configured model, and measured duration so no adapter or wall-clock state
leaks into this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.domain.events.events import (
    WorkerCostItem,
    WorkerResponseLatencyItem,
    WorkerTokenUsageItem,
    WorkerUsageMetrics,
)

from .shared import UsageBreakdown, emit_cost


if TYPE_CHECKING:
    from core.domain.events.events import DomainEvent

    from .shared import EventSequencer


@dataclass(slots=True)
class OpenHandsCostData:
    """Normalized OpenHands cost data used to build WorkerCostRecorded events."""

    cost_usd: float = 0.0
    tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_metrics: list[WorkerUsageMetrics] = field(default_factory=list)


def make_cost_recorded_event(
    conversation: Any,
    sequencer: EventSequencer,
    *,
    tool_name: str,
    model: str | None,
    duration_seconds: float,
    container_id: str | None = None,
    conversation_id: str | None = None,
    complete: bool = True,
    termination_reason: str = "completed",
) -> DomainEvent:
    cost_data = extract_cost_data(conversation)
    return emit_cost(
        sequencer,
        tool_name=tool_name,
        duration_seconds=duration_seconds,
        model=resolve_cost_model(cost_data, model),
        breakdown=UsageBreakdown(
            prompt_tokens=cost_data.prompt_tokens,
            completion_tokens=cost_data.completion_tokens,
            cache_read_tokens=cost_data.cache_read_tokens,
            cache_write_tokens=cost_data.cache_write_tokens,
            reasoning_tokens=cost_data.reasoning_tokens,
            cost_usd=cost_data.cost_usd,
        ),
        usage_metrics=cost_data.usage_metrics,
        container_id=container_id,
        conversation_id=conversation_id,
        complete=complete,
        termination_reason=termination_reason,
    )


def extract_cost_data(conversation: Any) -> OpenHandsCostData:
    stats = getattr(conversation, "conversation_stats", None)
    if stats is None:
        return OpenHandsCostData()

    usage_to_metrics = _get_stat(stats, "usage_to_metrics")
    return _extract_usage_metrics(usage_to_metrics) if usage_to_metrics else OpenHandsCostData()


def resolve_cost_model(cost_data: OpenHandsCostData, default_model: str | None) -> str | None:
    usage_models = {usage.model for usage in cost_data.usage_metrics if usage.model}
    if len(usage_models) == 1:
        return next(iter(usage_models))
    return default_model


def _extract_usage_metrics(usage_to_metrics: Any) -> OpenHandsCostData:
    items = usage_to_metrics.items() if isinstance(usage_to_metrics, dict) else []
    usage_metrics: list[WorkerUsageMetrics] = []
    total_cost = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0

    for usage_id, metrics in items:
        usage_metric = _build_usage_metrics(str(usage_id), metrics)
        usage_metrics.append(usage_metric)
        total_cost += usage_metric.accumulated_cost_usd
        prompt_tokens += usage_metric.prompt_tokens
        completion_tokens += usage_metric.completion_tokens
        cache_read_tokens += usage_metric.cache_read_tokens
        cache_write_tokens += usage_metric.cache_write_tokens
        reasoning_tokens += usage_metric.reasoning_tokens

    if not usage_metrics:
        return OpenHandsCostData()

    return OpenHandsCostData(
        cost_usd=total_cost,
        tokens=UsageBreakdown(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        ).tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        usage_metrics=usage_metrics,
    )


def _build_usage_metrics(usage_id: str, metrics: Any) -> WorkerUsageMetrics:
    """Normalize one Metrics or MetricsSnapshot record into event payload data."""
    model = _get_stat(metrics, "model_name")
    accumulated_cost = float(_get_stat(metrics, "accumulated_cost", 0.0) or 0.0)
    (
        prompt_tokens,
        completion_tokens,
        cache_read_tokens,
        cache_write_tokens,
        reasoning_tokens,
    ) = _extract_accumulated_token_usage(metrics)

    return WorkerUsageMetrics(
        usage_id=usage_id,
        model=model,
        accumulated_cost_usd=accumulated_cost,
        prompt_tokens=prompt_tokens or 0,
        completion_tokens=completion_tokens or 0,
        cache_read_tokens=cache_read_tokens or 0,
        cache_write_tokens=cache_write_tokens or 0,
        reasoning_tokens=reasoning_tokens or 0,
        cost_items=[
            WorkerCostItem(
                model=_get_stat(cost, "model", "") or model or "",
                cost_usd=float(_get_stat(cost, "cost_usd", _get_stat(cost, "cost", 0.0)) or 0.0),
                timestamp=_maybe_float(_get_stat(cost, "timestamp")),
            )
            for cost in _get_stat(metrics, "costs", []) or []
        ],
        response_latencies=[
            WorkerResponseLatencyItem(
                model=_get_stat(latency, "model", "") or model or "",
                latency_seconds=float(_get_stat(latency, "latency", 0.0) or 0.0),
                response_id=str(_get_stat(latency, "response_id", "") or ""),
            )
            for latency in _get_stat(metrics, "response_latencies", []) or []
        ],
        token_usages=[
            WorkerTokenUsageItem(
                model=_get_stat(token_usage, "model", "") or model or "",
                prompt_tokens=int(_get_stat(token_usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(_get_stat(token_usage, "completion_tokens", 0) or 0),
                cache_read_tokens=int(_get_stat(token_usage, "cache_read_tokens", 0) or 0),
                cache_write_tokens=int(_get_stat(token_usage, "cache_write_tokens", 0) or 0),
                reasoning_tokens=int(_get_stat(token_usage, "reasoning_tokens", 0) or 0),
                context_window=int(_get_stat(token_usage, "context_window", 0) or 0),
                per_turn_token=int(_get_stat(token_usage, "per_turn_token", 0) or 0),
                response_id=str(_get_stat(token_usage, "response_id", "") or ""),
            )
            for token_usage in _get_stat(metrics, "token_usages", []) or []
        ],
    )


def _extract_accumulated_token_usage(
    stats: Any,
) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    accumulated = _get_stat(stats, "accumulated_token_usage")
    if accumulated is not None:
        return (
            _maybe_int(_get_stat(accumulated, "prompt_tokens")),
            _maybe_int(_get_stat(accumulated, "completion_tokens")),
            _maybe_int(_get_stat(accumulated, "cache_read_tokens")),
            _maybe_int(_get_stat(accumulated, "cache_write_tokens")),
            _maybe_int(_get_stat(accumulated, "reasoning_tokens")),
        )

    return (
        _maybe_int(_get_stat(stats, "prompt_tokens")),
        _maybe_int(_get_stat(stats, "completion_tokens")),
        _maybe_int(_get_stat(stats, "cache_read_tokens")),
        _maybe_int(_get_stat(stats, "cache_write_tokens")),
        _maybe_int(_get_stat(stats, "reasoning_tokens")),
    )


def _get_stat(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute or dict key without assuming an SDK object shape."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _maybe_int(value: Any) -> int | None:
    """Normalize optional numeric values to integers."""
    if value is None:
        return None
    return int(value)


def _maybe_float(value: Any) -> float | None:
    """Normalize optional numeric values to floats."""
    if value is None:
        return None
    return float(value)
