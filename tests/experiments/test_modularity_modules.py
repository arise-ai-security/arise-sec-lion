"""Unit tests for node-tree reconstruction and module assignment."""

from __future__ import annotations

import pytest

from experiments.shared.scripts.analysis.modularity.modules import (
    BOSS,
    UNKNOWN,
    build_labeled_modules,
    detect_branch_from_task,
)
from experiments.shared.scripts.analysis.modularity.nodes import build_node_table
from experiments.shared.scripts.db.models import EventRow


def _ev(agg: str, seq: int, etype: str, **payload) -> EventRow:
    return EventRow(f"{agg}-{seq}", agg, seq, etype, payload, "2026-05-16T00:00:00Z", {})


def _tree_events() -> list[EventRow]:
    # boss -> m (manager, [Builder]) -> w (worker)
    return [
        _ev("boss", 1, "AgentCreated", role="boss", parent_id=None),
        _ev("boss", 2, "ChildSpawned", child_id="m", subtask={"description": "[Builder] build it"}),
        _ev("m", 1, "AgentCreated", role="pending", parent_id="boss", sibling_index="0",
            success_criteria="binary exists"),
        _ev("m", 2, "TaskAssigned", task_description="[Builder] build it"),
        _ev("m", 3, "ComplexityEvaluated", determined_role="manager"),
        _ev("m", 4, "ChildSpawned", child_id="w", subtask={"description": "compile"}),
        _ev("w", 1, "AgentCreated", role="worker", parent_id="m", sibling_index="0"),
        _ev("w", 2, "TaskAssigned", task_description="compile the target"),
    ]


def test_detect_branch_bracket_beats_keyword():
    # Given: a Fixer task that also contains the word "exploit"
    # When/Then: the bracket prefix is authoritative
    assert detect_branch_from_task("[Fixer] patch the exploit path") == "fixer"


def test_detect_branch_keyword_fallback():
    assert detect_branch_from_task("Create a proof-of-concept exploit") == "exploiter"
    assert detect_branch_from_task("Set up the build environment") == "builder"
    assert detect_branch_from_task("Synthesize the security report") == "reporter"


def test_detect_branch_none_for_generic():
    assert detect_branch_from_task("do something unrelated") is None


def test_detect_branch_parity_with_source():
    # Given: the system's authoritative detector
    src = pytest.importorskip("plugins.security.prompt_strategy")
    samples = [
        "[Builder] build libplist", "[Exploiter] craft PoC", "[Fixer] minimal patch",
        "[Reporter] synthesize report", "set up the environment", "write an exploit",
        "apply a fix to the patch", "security report time", "totally generic task", "",
    ]
    # When/Then: our port agrees with the source on every sample
    for text in samples:
        assert detect_branch_from_task(text) == src._detect_branch_from_task(text)


def test_build_node_table_depth_parent_role():
    # Given: a 3-level tree
    table = build_node_table(_tree_events(), "boss")
    # When/Then: depths, parents, children, and overridden role are correct
    assert table.nodes["boss"].depth == 0
    assert table.nodes["m"].depth == 1
    assert table.nodes["w"].depth == 2
    assert table.nodes["m"].parent_id == "boss"
    assert table.nodes["boss"].n_children == 1
    assert table.nodes["m"].role_declared == "manager"  # ComplexityEvaluated override
    assert table.module_root_of("w") == "m"
    assert table.module_root_of("m") == "m"
    assert table.module_root_of("boss") is None


def test_labeled_modules_propagate_to_subtree():
    # Given: the labeled-module assignment for the 3-level tree
    table = build_node_table(_tree_events(), "boss")
    assign = build_labeled_modules(_tree_events(), table)
    # When/Then: boss is "boss"; the worker inherits its depth-1 ancestor's branch
    assert assign.module_of("boss") == BOSS
    assert assign.module_of("m") == "builder"
    assert assign.module_of("w") == "builder"
    assert assign.conflicts == {}


def test_labeled_modules_unknown_for_unbracketed_root():
    # Given: a depth-1 root whose task carries no branch signal
    events = [
        _ev("boss", 1, "AgentCreated", role="boss", parent_id=None),
        _ev("boss", 2, "ChildSpawned", child_id="x", subtask={"description": "mystery"}),
        _ev("x", 1, "AgentCreated", role="pending", parent_id="boss", sibling_index="0"),
        _ev("x", 2, "TaskAssigned", task_description="mystery work"),
    ]
    table = build_node_table(events, "boss")
    assign = build_labeled_modules(events, table)
    # When/Then: the unlabeled subtree is "unknown", not silently mislabeled
    assert assign.module_of("x") == UNKNOWN
