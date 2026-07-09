"""Public evaluation API for hierarchical BEF runs (B3, B4).

Use :class:`BefRunEvaluator` to load a run once and compute many metrics; or call
a standalone ``await <metric>(run_id)`` function when you want a single result.
All computation is delegated to the pure concern modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from experiments.shared.evaluation import cache, cost, criteria, prompts, tools
from experiments.shared.evaluation.loading import load_run


if TYPE_CHECKING:
    from uuid import UUID

    from core.domain.events.events import DomainEvent
    from experiments.shared.evaluation.models import (
        ArtifactsBySubtree,
        CostByRole,
        CountBreakdown,
        RateBreakdown,
        RunData,
        WorkerPrompts,
    )


@dataclass(frozen=True, slots=True)
class BefRunEvaluator:
    """All BEF metrics for one loaded run (load once, call many)."""

    run_data: RunData

    @classmethod
    async def load(cls, run_id: str | UUID) -> BefRunEvaluator:
        """Load a run from the event store and wrap it for evaluation."""
        return cls(await load_run(run_id))

    # shared
    def events(self) -> list[DomainEvent]:
        return self.run_data.events

    # cost
    def total_cost_usd(self) -> float:
        return cost.total_cost_usd(self.run_data)

    def cost_by_role(self) -> CostByRole:
        return cost.cost_by_role(self.run_data)

    # prompts
    def worker_prompts(self) -> WorkerPrompts:
        return prompts.worker_prompts(self.run_data)

    # tools
    def tool_call_total(self) -> int:
        return tools.tool_call_total(self.run_data)

    def tool_calls_by_bef(self) -> CountBreakdown:
        return tools.tool_calls_by_bef(self.run_data)

    def tool_calls_by_category(self) -> CountBreakdown:
        return tools.tool_calls_by_category(self.run_data)

    def tool_calls_by_node(self) -> CountBreakdown:
        return tools.tool_calls_by_node(self.run_data)

    # cache
    def cache_rate_by_bef(self) -> RateBreakdown:
        return cache.cache_rate_by_bef(self.run_data)

    def cache_rate_by_node(self) -> RateBreakdown:
        return cache.cache_rate_by_node(self.run_data)

    # criteria
    def artifacts_by_bef(self) -> ArtifactsBySubtree:
        return criteria.artifacts_by_bef(self.run_data)

    def success_criteria_by_bef(self) -> dict[str, dict[str, Any]]:
        return criteria.success_criteria_by_bef(self.run_data)


# ---------------------------------------------------------------------------
# Standalone "input: run_id" functions (each loads the run once)
# ---------------------------------------------------------------------------


async def get_run_events(run_id: str | UUID) -> list[DomainEvent]:
    """All events for the run (shared #1)."""
    return (await load_run(run_id)).events


async def total_cost_usd(run_id: str | UUID) -> float:
    """Total run cost in USD (cost #2)."""
    return cost.total_cost_usd(await load_run(run_id))


async def cost_by_role(run_id: str | UUID) -> CostByRole:
    """Cost in USD by node role (cost #3)."""
    return cost.cost_by_role(await load_run(run_id))


async def worker_prompts(run_id: str | UUID) -> WorkerPrompts:
    """Concatenated, prettified worker prompts (prompt #4)."""
    return prompts.worker_prompts(await load_run(run_id))


async def tool_call_total(run_id: str | UUID) -> int:
    """Total tool-call count (tools #1)."""
    return tools.tool_call_total(await load_run(run_id))


async def tool_calls_by_bef(run_id: str | UUID) -> CountBreakdown:
    """Tool-call count by BEF subtree (tools #2)."""
    return tools.tool_calls_by_bef(await load_run(run_id))


async def tool_calls_by_category(run_id: str | UUID) -> CountBreakdown:
    """Tool-call count by tool category (tools #3)."""
    return tools.tool_calls_by_category(await load_run(run_id))


async def tool_calls_by_node(run_id: str | UUID) -> CountBreakdown:
    """Tool-call count by node role (tools #4)."""
    return tools.tool_calls_by_node(await load_run(run_id))


async def cache_rate_by_bef(run_id: str | UUID) -> RateBreakdown:
    """Cache-hit rate by BEF subtree (cache #5)."""
    return cache.cache_rate_by_bef(await load_run(run_id))


async def cache_rate_by_node(run_id: str | UUID) -> RateBreakdown:
    """Cache-hit rate by node role (cache #6)."""
    return cache.cache_rate_by_node(await load_run(run_id))


async def artifacts_by_bef(run_id: str | UUID) -> ArtifactsBySubtree:
    """Non-vacuous artifacts written/edited by each BEF subtree (criteria #5)."""
    return criteria.artifacts_by_bef(await load_run(run_id))


async def success_criteria_by_bef(run_id: str | UUID) -> dict[str, dict[str, Any]]:
    """Per-subtree success: key files, declared criteria, self-report (criteria #6)."""
    return criteria.success_criteria_by_bef(await load_run(run_id))
