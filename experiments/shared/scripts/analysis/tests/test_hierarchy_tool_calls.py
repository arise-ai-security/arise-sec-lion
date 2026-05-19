"""Tests for ``compute_hierarchy_tool_calls`` (hierarchical tool-call accounting).

Splits tool-use events into BOSS / MANAGER / WORKER buckets by walking the
``AgentCreated`` parent_id tree from the run's boss aggregate:
  depth == 0  ->  BOSS
  depth == 1  ->  MANAGER
  depth >= 2  ->  WORKER
"""

from __future__ import annotations

from uuid import uuid4

from experiments.shared.scripts.analysis.metrics.hierarchy import (
    compute_hierarchy_tool_calls,
)
from experiments.shared.scripts.analysis.tests.factories import (
    agent_created,
    child_spawned,
    run_started,
    tool_use_event,
)


def test_flat_run_assigns_all_tool_calls_to_boss() -> None:
    # Given: a flat-mode A1/A2-like run where the boss is the only agent and
    # the boss directly emits tool_use events.
    boss = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        tool_use_event(boss, 3, "Read", {"file_path": "/a"}, set_structured_field=True),
        tool_use_event(boss, 4, "Bash", {"command": "ls"}, set_structured_field=True),
    ]

    # When: hierarchy tool calls are computed
    metrics = compute_hierarchy_tool_calls(events, run_id=boss)

    # Then: all tool calls land in the BOSS bucket
    assert metrics.tool_calls_by_role == {"boss": 2, "manager": 0, "worker": 0}
    assert metrics.nodes_by_role == {"boss": 1, "manager": 0, "worker": 0}
    assert metrics.tool_calls_by_role_by_name["boss"] == {"Read": 1, "Bash": 1}
    assert metrics.tool_calls_by_role_by_name["manager"] == {}
    assert metrics.tool_calls_by_role_by_name["worker"] == {}


def test_three_tier_run_classifies_by_parent_depth() -> None:
    # Given: a B1-shaped run with boss -> manager -> worker, each emitting tool calls
    boss = uuid4()
    manager = uuid4()
    worker = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        child_spawned(boss, 3, child_id=manager, child_role="pending"),
        agent_created(manager, 1, role="pending", parent_id=boss),
        child_spawned(manager, 2, child_id=worker, child_role="pending"),
        agent_created(worker, 1, role="pending", parent_id=manager),
        tool_use_event(boss, 4, "Read", {"file_path": "/a"}, set_structured_field=True),
        tool_use_event(manager, 3, "Read", {"file_path": "/b"}, set_structured_field=True),
        tool_use_event(manager, 4, "Bash", {"command": "ls"}, set_structured_field=True),
        tool_use_event(worker, 2, "Bash", {"command": "ls"}, set_structured_field=True),
        tool_use_event(worker, 3, "Bash", {"command": "pwd"}, set_structured_field=True),
        tool_use_event(worker, 4, "Edit", {"file_path": "/x"}, set_structured_field=True),
    ]

    # When: hierarchy tool calls are computed
    metrics = compute_hierarchy_tool_calls(events, run_id=boss)

    # Then: depth 0 -> boss, depth 1 -> manager, depth 2 -> worker
    assert metrics.tool_calls_by_role == {"boss": 1, "manager": 2, "worker": 3}
    assert metrics.nodes_by_role == {"boss": 1, "manager": 1, "worker": 1}
    assert metrics.tool_calls_by_role_by_name == {
        "boss": {"Read": 1},
        "manager": {"Read": 1, "Bash": 1},
        "worker": {"Bash": 2, "Edit": 1},
    }


def test_orphaned_tool_use_event_lands_in_unknown_role() -> None:
    # Given: a tool_use event whose aggregate never had an AgentCreated.
    # We want this to be visible, not silently dropped.
    boss = uuid4()
    orphan = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        tool_use_event(orphan, 1, "Read", {"file_path": "/a"}, set_structured_field=True),
    ]

    # When: hierarchy tool calls are computed
    metrics = compute_hierarchy_tool_calls(events, run_id=boss)

    # Then: the orphan event is bucketed under "unknown", boss has no calls
    assert metrics.tool_calls_by_role["boss"] == 0
    assert metrics.tool_calls_by_role.get("unknown", 0) == 1
    assert metrics.tool_calls_by_role_by_name.get("unknown", {}) == {"Read": 1}


def test_non_tool_use_thoughts_are_ignored() -> None:
    # Given: ThoughtCaptured events whose output_type is "thinking" (not tool_use)
    boss = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        # tool_use_event with set_structured_field uses output_type=tool_use; we want
        # to test the negative case here so we manually craft a thinking event
        tool_use_event(boss, 3, "Read", {"file_path": "/a"}, set_structured_field=True),
    ]

    # Mutate the last event so output_type is "thinking" -- mirrors the
    # claude_sdk path where the same content type can carry a thinking trace.
    from dataclasses import replace as dc_replace

    thinking_event = dc_replace(
        events[-1],
        payload={**events[-1].payload, "output_type": "thinking"},
    )
    events = [*events[:-1], thinking_event]

    # When: hierarchy tool calls are computed
    metrics = compute_hierarchy_tool_calls(events, run_id=boss)

    # Then: the thinking event is not counted at all
    assert metrics.tool_calls_by_role == {"boss": 0, "manager": 0, "worker": 0}
