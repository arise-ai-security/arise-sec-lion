"""Cost metrics: total USD and per-role breakdown.

Cost is the sum of LLM-side ``TokensConsumed.cost_usd`` (boss/manager decomposition
calls) and worker-side ``WorkerCostRecorded.cost_usd`` (tool executions).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from core.domain.events.events import TokensConsumed, WorkerCostRecorded
from core.domain.exceptions import CostInvariantViolation
from experiments.shared.evaluation.common import (
    ROLE_KEYS,
    UNKNOWN_ROLE,
    build_role_map,
    events_of_type,
    role_key,
)
from experiments.shared.evaluation.models import CostByRole


if TYPE_CHECKING:
    from experiments.shared.evaluation.models import RunData

_ABS_TOL = 1e-4
_REL_TOL = 1e-6


def llm_worker_costs(run_data: RunData) -> tuple[float, float]:
    """Return ``(llm_cost_usd, worker_cost_usd)`` for the run."""
    llm = sum((e.cost_usd for e in events_of_type(run_data.events, TokensConsumed)), 0.0)
    worker = sum((e.cost_usd for e in events_of_type(run_data.events, WorkerCostRecorded)), 0.0)
    return llm, worker


def total_cost_usd(run_data: RunData) -> float:
    """Total run cost in USD (LLM + worker)."""
    llm, worker = llm_worker_costs(run_data)
    return llm + worker


def cost_by_role(run_data: RunData) -> CostByRole:
    """Cost in USD attributed to each node role.

    Each cost event is attributed to its agent's role (``aggregate_id → role``);
    unresolved agents fall into ``UNKNOWN``. The per-role values are guaranteed to
    sum to the total (a ``CostInvariantViolation`` is raised otherwise).
    """
    role_map = build_role_map(run_data.events)
    by_role: dict[str, float] = dict.fromkeys((*ROLE_KEYS, UNKNOWN_ROLE), 0.0)

    llm = 0.0
    for event in events_of_type(run_data.events, TokensConsumed):
        key = role_key(role_map, event.aggregate_id)
        by_role[key] = by_role.get(key, 0.0) + event.cost_usd
        llm += event.cost_usd

    worker = 0.0
    for event in events_of_type(run_data.events, WorkerCostRecorded):
        key = role_key(role_map, event.aggregate_id)
        by_role[key] = by_role.get(key, 0.0) + event.cost_usd
        worker += event.cost_usd

    total = llm + worker
    role_sum = sum(by_role.values())
    if not math.isclose(role_sum, total, rel_tol=_REL_TOL, abs_tol=_ABS_TOL):
        raise CostInvariantViolation(
            "sum(cost_by_role) == total_cost_usd",
            expected=total,
            actual=role_sum,
            context={"by_role": by_role},
        )
    return CostByRole(total_usd=total, llm_usd=llm, worker_usd=worker, by_role=by_role)
