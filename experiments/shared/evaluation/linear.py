"""Public evaluation API for linear runs (N1, N2).

N1/N2 run one flat agent that owns all four BEF phases, so there are no per-phase
agents: the "by subtree" metrics collapse to a single ``linear`` bucket. The
shared metrics (cost, prompts, tool/category/node counts, cache) reuse the same
pure concern functions as the BEF family.

Caveat: N2's OpenHands native subagents run inside the SDK session and emit no
domain events, so their tool calls and cost are invisible here — these functions
report only the top-level flat agent's recorded activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.domain.events.events import (
    AgentCreated,
    VerificationFailed,
    VerificationPassed,
    WorkCompleted,
    WorkFailed,
)
from experiments.shared.evaluation import cache, cost, criteria, prompts, tools
from experiments.shared.evaluation.common import events_of_type, is_vacuous
from experiments.shared.evaluation.loading import load_run
from experiments.shared.evaluation.models import (
    ArtifactRef,
    ArtifactsBySubtree,
    BefPhase,
    CountBreakdown,
    RateBreakdown,
)


if TYPE_CHECKING:
    from uuid import UUID

    from core.domain.events.events import DomainEvent
    from experiments.shared.evaluation.models import CostByRole, RunData, WorkerPrompts

_LINEAR = BefPhase.LINEAR.value  # "linear"


# ---------------------------------------------------------------------------
# Linear-specific re-bucketing (the "by subtree" analogs)
# ---------------------------------------------------------------------------


def _tool_calls_linear(run_data: RunData) -> CountBreakdown:
    total = tools.tool_call_total(run_data)
    return CountBreakdown(total=total, by={_LINEAR: total} if total else {})


def _cache_rate_linear(run_data: RunData) -> RateBreakdown:
    overall = cache.cache_rate_by_node(run_data).overall
    return RateBreakdown(overall=overall, by={_LINEAR: overall})


def _artifacts_linear(run_data: RunData) -> ArtifactsBySubtree:
    """Non-vacuous testcase deliverables on disk, attributed to the single agent.

    Flat (N1/N2) runs do not emit ``SourceFileEdited`` events, so per-file
    provenance is unavailable. Since one agent owns the whole run, we list the
    on-disk ``testcase/`` deliverables (excluding vacuous files) as its artifacts.
    """
    testcase_dir = run_data.run_dir / "testcase"
    if not testcase_dir.is_dir():
        return ArtifactsBySubtree(by={})
    refs = [
        ArtifactRef(
            path=f"/testcase/{path.name}",
            disk_path=str(path),
            size_bytes=path.stat().st_size,
            edited_by=run_data.run_id,
        )
        for path in sorted(testcase_dir.iterdir())
        if path.is_file() and not is_vacuous(path)
    ]
    return ArtifactsBySubtree(by={_LINEAR: refs} if refs else {})


def _success_linear(run_data: RunData) -> dict[str, dict[str, Any]]:
    declared: list[str] = []
    for created in events_of_type(run_data.events, AgentCreated):
        criterion = created.success_criteria.strip()
        if criterion and criterion not in declared:
            declared.append(criterion)
    self_report = {
        "work_completed": [e.result for e in events_of_type(run_data.events, WorkCompleted)],
        "work_failed": [e.reason for e in events_of_type(run_data.events, WorkFailed)],
        "verification_passed": sum(1 for _ in events_of_type(run_data.events, VerificationPassed)),
        "verification_failed": [
            {"stage": e.failed_stage, "feedback": e.feedback, "score": e.score}
            for e in events_of_type(run_data.events, VerificationFailed)
        ],
    }
    return {
        _LINEAR: {
            "key_files_exist": {
                spec: criteria.key_file_exists(spec, run_data.run_dir)
                for spec in criteria.ALL_KEY_FILES
            },
            "declared_criteria": declared,
            "self_report": self_report,
        }
    }


@dataclass(frozen=True, slots=True)
class LinearRunEvaluator:
    """All linear (N1/N2) metrics for one loaded run (load once, call many)."""

    run_data: RunData

    @classmethod
    async def load(cls, run_id: str | UUID) -> LinearRunEvaluator:
        """Load a run from the event store and wrap it for evaluation."""
        return cls(await load_run(run_id))

    def events(self) -> list[DomainEvent]:
        return self.run_data.events

    def total_cost_usd(self) -> float:
        return cost.total_cost_usd(self.run_data)

    def cost_by_role(self) -> CostByRole:
        return cost.cost_by_role(self.run_data)

    def worker_prompts(self) -> WorkerPrompts:
        return prompts.worker_prompts(self.run_data)

    def tool_call_total(self) -> int:
        return tools.tool_call_total(self.run_data)

    def tool_calls_by_subtree(self) -> CountBreakdown:
        return _tool_calls_linear(self.run_data)

    def tool_calls_by_category(self) -> CountBreakdown:
        return tools.tool_calls_by_category(self.run_data)

    def tool_calls_by_node(self) -> CountBreakdown:
        return tools.tool_calls_by_node(self.run_data)

    def cache_rate_by_subtree(self) -> RateBreakdown:
        return _cache_rate_linear(self.run_data)

    def cache_rate_by_node(self) -> RateBreakdown:
        return cache.cache_rate_by_node(self.run_data)

    def artifacts_by_subtree(self) -> ArtifactsBySubtree:
        return _artifacts_linear(self.run_data)

    def success_criteria_by_subtree(self) -> dict[str, dict[str, Any]]:
        return _success_linear(self.run_data)


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
    """The flat agent's worker prompt, prettified (prompt #4)."""
    return prompts.worker_prompts(await load_run(run_id))


async def tool_call_total(run_id: str | UUID) -> int:
    """Total tool-call count (tools #1)."""
    return tools.tool_call_total(await load_run(run_id))


async def tool_calls_by_subtree(run_id: str | UUID) -> CountBreakdown:
    """Tool-call count in the single linear bucket (tools #2, linear)."""
    return _tool_calls_linear(await load_run(run_id))


async def tool_calls_by_category(run_id: str | UUID) -> CountBreakdown:
    """Tool-call count by tool category (tools #3)."""
    return tools.tool_calls_by_category(await load_run(run_id))


async def tool_calls_by_node(run_id: str | UUID) -> CountBreakdown:
    """Tool-call count by node role (tools #4)."""
    return tools.tool_calls_by_node(await load_run(run_id))


async def cache_rate_by_subtree(run_id: str | UUID) -> RateBreakdown:
    """Cache-hit rate in the single linear bucket (cache #5, linear)."""
    return _cache_rate_linear(await load_run(run_id))


async def cache_rate_by_node(run_id: str | UUID) -> RateBreakdown:
    """Cache-hit rate by node role (cache #6)."""
    return cache.cache_rate_by_node(await load_run(run_id))


async def artifacts_by_subtree(run_id: str | UUID) -> ArtifactsBySubtree:
    """Non-vacuous artifacts written/edited, in the single linear bucket (criteria #5)."""
    return _artifacts_linear(await load_run(run_id))


async def success_criteria_by_subtree(run_id: str | UUID) -> dict[str, dict[str, Any]]:
    """Linear success: all key files, declared criteria, self-report (criteria #6)."""
    return _success_linear(await load_run(run_id))
