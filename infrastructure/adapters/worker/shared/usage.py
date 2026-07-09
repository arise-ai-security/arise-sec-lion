"""Shared worker usage normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.domain.events.events import WorkerCostRecorded, WorkerUsageMetrics


if TYPE_CHECKING:
    from .event_sequencer import EventSequencer


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    """Normalized worker usage buckets.

    The inclusive total is derived from the five bucket fields. Worker SDK
    aggregate totals are intentionally ignored because some providers report
    prompt+completion only and omit cache or reasoning tokens.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def tokens(self) -> int | None:
        parts = (
            self.prompt_tokens,
            self.completion_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )
        if not any(part is not None for part in parts):
            return None
        return sum(part or 0 for part in parts)

    @property
    def has_cost_data(self) -> bool:
        return self.cost_usd is not None or self.tokens is not None


def emit_cost(
    sequencer: EventSequencer,
    *,
    tool_name: str,
    model: str | None,
    breakdown: UsageBreakdown,
    duration_seconds: float,
    usage_metrics: list[WorkerUsageMetrics] | None = None,
    container_id: str | None = None,
    conversation_id: str | None = None,
) -> WorkerCostRecorded:
    """Create a cost event from a normalized usage breakdown."""
    return sequencer.cost_recorded(
        tool_name=tool_name,
        cost_usd=breakdown.cost_usd or 0.0,
        duration_seconds=duration_seconds,
        model=model,
        tokens=breakdown.tokens,
        prompt_tokens=breakdown.prompt_tokens,
        completion_tokens=breakdown.completion_tokens,
        cache_read_tokens=breakdown.cache_read_tokens,
        cache_write_tokens=breakdown.cache_write_tokens,
        reasoning_tokens=breakdown.reasoning_tokens,
        usage_metrics=usage_metrics,
        container_id=container_id,
        conversation_id=conversation_id,
    )
