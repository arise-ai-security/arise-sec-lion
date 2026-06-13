"""Tests for cost metrics."""

from uuid import uuid4

import pytest

from experiments.shared.evaluation import cost
from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.tests.builders import RunBuilder


def _b4_costed() -> RunBuilder:
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Builder] build")
    worker = builder.agent("worker", manager, "[Build-Setup] setup")
    builder.tokens(boss, cost=0.10, prompt=1000, cache_read=500, operation="task_decomposition")
    builder.tokens(manager, cost=0.05, prompt=500, cache_read=100, operation="task_assessment")
    builder.worker_cost(worker, cost=0.80, prompt=2000, cache_read=1800)
    return builder


def test_total_cost_sums_llm_and_worker(tmp_path) -> None:
    """Total cost = sum of TokensConsumed + WorkerCostRecorded."""
    # Given
    run_data = _b4_costed().run_data(tmp_path)
    # When/Then
    assert cost.total_cost_usd(run_data) == pytest.approx(0.95)


def test_cost_by_role_attributes_and_sums(tmp_path) -> None:
    """Each cost is attributed to its agent's role and the parts sum to the total."""
    # Given
    run_data = _b4_costed().run_data(tmp_path)
    # When
    result = BefRunEvaluator(run_data).cost_by_role()
    # Then
    assert result.total_usd == pytest.approx(0.95)
    assert result.llm_usd == pytest.approx(0.15)
    assert result.worker_usd == pytest.approx(0.80)
    assert result.by_role["BOSS"] == pytest.approx(0.10)
    assert result.by_role["MANAGER"] == pytest.approx(0.05)
    assert result.by_role["WORKER"] == pytest.approx(0.80)
    # And: invariant — roles sum to total
    assert sum(result.by_role.values()) == pytest.approx(result.total_usd)


def test_cost_for_agent_without_creation_is_unknown(tmp_path) -> None:
    """A cost event on an aggregate with no AgentCreated lands in UNKNOWN."""
    # Given
    builder = RunBuilder()
    builder.boss()
    orphan = uuid4()
    builder.worker_cost(orphan, cost=0.20, prompt=10)
    # When
    result = cost.cost_by_role(builder.run_data(tmp_path))
    # Then
    assert result.by_role["UNKNOWN"] == pytest.approx(0.20)
    assert sum(result.by_role.values()) == pytest.approx(result.total_usd)
