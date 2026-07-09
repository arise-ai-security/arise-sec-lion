"""Tests for worker-prompt assembly."""

from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.models import BefPhase
from experiments.shared.evaluation.tests.builders import RunBuilder


def test_worker_prompts_collects_only_worker_execution(tmp_path) -> None:
    """Only worker_execution prompts are collected, labeled with phase + role."""
    # Given: a decomposition (LLM) prompt and a worker prompt
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Builder] build")
    worker = builder.agent("worker", manager, "[Build-Setup] setup")
    builder.prompt(boss, "decompose this", prompt_type="task_decomposition", target="llm")
    builder.prompt(worker, "WORKER PROMPT BODY", prompt_type="worker_execution", target="openhands")
    # When
    result = BefRunEvaluator(builder.run_data(tmp_path)).worker_prompts()
    # Then
    assert len(result.prompts) == 1
    only = result.prompts[0]
    assert only.phase is BefPhase.BUILDER
    assert only.role_label == "Build-Setup"
    assert only.target == "openhands"
    # And: the prettified text includes the worker prompt and excludes the LLM one
    assert "WORKER PROMPT BODY" in result.text
    assert "decompose this" not in result.text
    assert "Build-Setup" in result.text


def test_worker_prompts_preserve_execution_order(tmp_path) -> None:
    """Multiple worker prompts appear in execution (time) order."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    first = builder.agent("worker", boss, "[Builder] one")
    second = builder.agent("worker", boss, "[Exploiter] two")
    builder.prompt(first, "FIRST")
    builder.prompt(second, "SECOND")
    # When
    result = BefRunEvaluator(builder.run_data(tmp_path)).worker_prompts()
    # Then
    assert [p.prompt for p in result.prompts] == ["FIRST", "SECOND"]
    assert result.text.index("FIRST") < result.text.index("SECOND")
