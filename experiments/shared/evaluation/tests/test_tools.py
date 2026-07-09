"""Tests for tool-call metrics."""

from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.tests.builders import RunBuilder, file_editor, grep, shell


def _run() -> RunBuilder:
    """Builder: 2 BEF subtrees with a worker each, plus thoughts/finish noise."""
    builder = RunBuilder()
    boss = builder.boss()
    build_mgr = builder.agent("manager", boss, "[Builder] build")
    build_worker = builder.agent("worker", build_mgr, "[Build-Setup] setup")
    exploit_mgr = builder.agent("manager", boss, "[Exploiter] exploit")
    exploit_worker = builder.agent("worker", exploit_mgr, "[PoC-Researcher] research")
    # Builder worker: 2 reads + 1 write + finish (finish excluded from totals)
    builder.tool(build_worker, file_editor("view"))
    builder.tool(build_worker, file_editor("view"))
    builder.tool(build_worker, file_editor("create"))
    builder.finish(build_worker)
    # Exploiter worker: 1 shell + 1 grep + a thought (thought excluded)
    builder.tool(exploit_worker, shell())
    builder.tool(exploit_worker, grep())
    builder.thought(exploit_worker, "thinking")
    return builder


def test_tool_call_total_excludes_finish_and_thoughts(tmp_path) -> None:
    """Total counts real tool calls only (5): excludes Finish and thoughts."""
    # Given/When/Then
    assert BefRunEvaluator(_run().run_data(tmp_path)).tool_call_total() == 5


def test_by_category_shows_finish_but_total_excludes_it(tmp_path) -> None:
    """Category breakdown surfaces Finish; total stays consistent with the count."""
    # When
    result = BefRunEvaluator(_run().run_data(tmp_path)).tool_calls_by_category()
    # Then
    assert result.by["FileRead"] == 2
    assert result.by["FileWrite"] == 1
    assert result.by["Bash"] == 1
    assert result.by["Grep"] == 1
    assert result.by["Finish"] == 1
    assert result.total == 5  # Finish excluded


def test_by_bef_groups_by_phase(tmp_path) -> None:
    """Counts group by BEF subtree and sum to the total."""
    # When
    result = BefRunEvaluator(_run().run_data(tmp_path)).tool_calls_by_bef()
    # Then
    assert result.by == {"Builder": 3, "Exploiter": 2}
    assert result.total == 5


def test_by_node_groups_by_role(tmp_path) -> None:
    """All counted calls here belong to WORKER agents."""
    # When
    result = BefRunEvaluator(_run().run_data(tmp_path)).tool_calls_by_node()
    # Then
    assert result.by == {"WORKER": 5}
    assert result.total == 5
