"""Frozen dataclasses for run-result quantitative metrics (design doc §5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True)
class CostMetrics:
    manager_cost_usd: float
    worker_cost_usd: float
    total_cost_usd: float
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    llm_call_count: int
    manager_cost_by_model: dict[str, float]
    worker_cost_by_model: dict[str, float]
    cost_by_model: dict[str, float]
    cost_by_operation: dict[str, float]

    @property
    def total_llm_cost_usd(self) -> float:
        """Backward-compatible name for manager/orchestration LLM cost."""
        return self.manager_cost_usd

    @property
    def total_worker_cost_usd(self) -> float:
        """Backward-compatible name for worker-side LLM cost."""
        return self.worker_cost_usd


@dataclass(frozen=True)
class WebViolation:
    via: str
    tool_or_cmd: str
    evidence_snippet: str
    aggregate_id: UUID
    sequence_number: int


@dataclass(frozen=True)
class ToolMetrics:
    total_tool_calls: int
    by_tool_name: dict[str, int]
    by_category: dict[str, int]
    bash_subtypes: dict[str, int]
    subagent_spawn_count: int
    forbidden_web_attempts: int
    forbidden_web_breakdown: dict[str, int]
    forbidden_web_violations: tuple[WebViolation, ...]


@dataclass(frozen=True)
class OutcomeMetrics:
    run_status: str
    has_run_completed: bool
    has_work_completed: bool
    has_work_failed: bool
    work_completed_count: int
    work_failed_count: int
    verification_passed_count: int
    verification_failed_count: int
    verification_pass_rate: float | None
    verification_failed_stages: dict[str, int]
    retries: int
    redecompositions: int
    decisions_infeasible: int
    failure_mode: str | None
    failure_reason: str | None
    worker_exit_status: str | None


@dataclass(frozen=True)
class HierarchyMetrics:
    num_agents: int
    max_depth: int
    max_fanout: int
    agents_by_role: dict[str, int]
    children_spawned: int
    children_completed: int
    children_failed: int


@dataclass(frozen=True)
class TimingMetrics:
    run_started_at: datetime | None
    run_completed_at: datetime | None
    wall_clock_seconds: float | None
    run_duration_seconds: float | None
    agent_execution_total_seconds: float
    operation_total_seconds: float
    operation_seconds_by_type: dict[str, float]


@dataclass(frozen=True)
class LimitMetrics:
    total_enforcements: int
    by_limit_type: dict[str, int]


@dataclass(frozen=True)
class QuantitativeMetrics:
    run_id: UUID
    family: str
    event_count: int
    aggregate_count: int
    cost: CostMetrics
    tools: ToolMetrics
    outcomes: OutcomeMetrics
    hierarchy: HierarchyMetrics
    timing: TimingMetrics
    limits: LimitMetrics


@dataclass(frozen=True)
class RunResult:
    run_id: UUID
    quantitative: QuantitativeMetrics
    qualitative: None = None
