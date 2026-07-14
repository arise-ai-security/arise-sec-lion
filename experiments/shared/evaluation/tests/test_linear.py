"""Tests for flat-run evaluation."""

import pytest

from experiments.shared.evaluation.linear import LinearRunEvaluator
from experiments.shared.evaluation.tests.builders import RunBuilder, file_editor, shell, write_files


def _linear_run() -> RunBuilder:
    """A single flat agent (the boss) owns all phases."""
    builder = RunBuilder()
    boss = builder.boss()
    builder.op_started(boss)
    builder.prompt(boss, "FLAT PIPELINE PROMPT")
    builder.tool(boss, file_editor("view"))
    builder.tool(boss, shell())
    builder.finish(boss)
    builder.worker_cost(boss, cost=0.7, prompt=1000, cache_read=800)
    builder.edited(boss, "/testcase/security_report.md")
    builder.completed(boss, "all phases done")
    return builder


def test_tool_calls_collapse_to_single_linear_bucket(tmp_path) -> None:
    """by_subtree returns one 'linear' bucket; node collapses to the boss agent."""
    # Given
    evaluator = LinearRunEvaluator(_linear_run().run_data(tmp_path))
    # Then
    assert evaluator.tool_call_total() == 2  # finish excluded
    assert evaluator.tool_calls_by_subtree().by == {"linear": 2}
    assert evaluator.tool_calls_by_node().by == {"BOSS": 2}


def test_cache_rate_single_bucket(tmp_path) -> None:
    """Cache rate is reported in the single linear bucket."""
    # When
    result = LinearRunEvaluator(_linear_run().run_data(tmp_path)).cache_rate_by_subtree()
    # Then
    assert result.by["linear"] == pytest.approx(0.8)
    assert result.overall == pytest.approx(0.8)


def test_artifacts_and_success_in_linear_bucket(tmp_path) -> None:
    """Artifacts and success collapse to a single 'linear' entry."""
    # Given
    run_dir = write_files(tmp_path, {"/testcase/security_report.md": b"# report"})
    evaluator = LinearRunEvaluator(_linear_run().run_data(run_dir))
    # When
    artifacts = evaluator.artifacts_by_subtree()
    success = evaluator.success_criteria_by_subtree()
    # Then
    assert [a.path for a in artifacts.by["linear"]] == ["/testcase/security_report.md"]
    linear = success["linear"]
    assert linear["key_files_exist"]["/testcase/security_report.md"] is True
    assert linear["self_report"]["work_completed"] == ["all phases done"]


def test_cost_attributed_to_single_boss_node(tmp_path) -> None:
    """The flat agent is the boss aggregate, so cost lands under BOSS."""
    # When
    result = LinearRunEvaluator(_linear_run().run_data(tmp_path)).cost_by_role()
    # Then
    assert result.total_usd == pytest.approx(0.7)
    assert result.by_role["BOSS"] == pytest.approx(0.7)
