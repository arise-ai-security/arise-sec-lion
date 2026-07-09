"""Unit tests for ``metrics.hierarchy`` (design doc §5)."""

from __future__ import annotations

from uuid import uuid4

from experiments.shared.scripts.analysis.metrics.hierarchy import compute_hierarchy
from experiments.shared.scripts.analysis.tests.factories import (
    agent_created,
    child_completed,
    child_failed,
    child_spawned,
    event_row,
    run_started,
)


def test_flat_mode_run_collapses_to_singleton_metrics():
    # Given: a flat-mode A1/A2-like run with only AgentCreated(role="boss")
    # on the boss aggregate and no ChildSpawned
    boss = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
    ]
    # When: hierarchy metrics are computed
    metrics = compute_hierarchy(events, run_id=boss)
    # Then: counts collapse to the degenerate single-agent case
    assert metrics.num_agents == 1
    assert metrics.max_depth == 0
    assert metrics.max_fanout == 0
    assert metrics.agents_by_role == {"boss": 1}
    assert metrics.children_spawned == 0
    assert metrics.children_completed == 0
    assert metrics.children_failed == 0


def test_two_level_tree_boss_manager_two_workers():
    # Given: boss -> manager -> {worker_a, worker_b}
    boss = uuid4()
    manager = uuid4()
    worker_a = uuid4()
    worker_b = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        child_spawned(boss, 3, child_id=manager, child_role="manager"),
        agent_created(manager, 1, role="manager", parent_id=boss),
        child_spawned(manager, 2, child_id=worker_a, child_role="worker"),
        child_spawned(manager, 3, child_id=worker_b, child_role="worker", sibling_index=1),
        agent_created(worker_a, 1, role="worker", parent_id=manager),
        agent_created(worker_b, 1, role="worker", parent_id=manager, sibling_index=1),
    ]
    # When: hierarchy metrics are computed
    metrics = compute_hierarchy(events, run_id=boss)
    # Then: tree-shape is faithfully recovered
    assert metrics.num_agents == 4
    assert metrics.max_depth == 2
    assert metrics.max_fanout == 2
    assert metrics.agents_by_role == {"boss": 1, "manager": 1, "worker": 2}
    assert metrics.children_spawned == 3
    assert metrics.children_completed == 0
    assert metrics.children_failed == 0


def test_max_fanout_picks_widest_parent():
    # Given: a boss with three direct children (workers)
    boss = uuid4()
    children = [uuid4() for _ in range(3)]
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        *[
            child_spawned(boss, 3 + i, child_id=cid, child_role="worker", sibling_index=i)
            for i, cid in enumerate(children)
        ],
        *[
            agent_created(cid, 1, role="worker", parent_id=boss, sibling_index=i)
            for i, cid in enumerate(children)
        ],
    ]
    # When: hierarchy metrics are computed
    metrics = compute_hierarchy(events, run_id=boss)
    # Then: max_fanout reflects the widest parent
    assert metrics.max_fanout == 3
    assert metrics.max_depth == 1
    assert metrics.num_agents == 4


def test_child_completed_and_failed_counts_are_tallied():
    # Given: boss spawns three children; two complete, one fails
    boss = uuid4()
    children = [uuid4() for _ in range(3)]
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        *[
            child_spawned(boss, 3 + i, child_id=cid, sibling_index=i)
            for i, cid in enumerate(children)
        ],
        child_completed(boss, 6, child_id=children[0]),
        child_completed(boss, 7, child_id=children[1]),
        child_failed(boss, 8, child_id=children[2], reason="boom"),
    ]
    # When: hierarchy metrics are computed
    metrics = compute_hierarchy(events, run_id=boss)
    # Then: completion / failure counters reflect the emitted events
    assert metrics.children_spawned == 3
    assert metrics.children_completed == 2
    assert metrics.children_failed == 1


def test_cycle_in_child_map_terminates_with_finite_depth():
    # Given: a synthetic malformed history where a grandchild incorrectly
    # links back to its grandparent (the root). compute_hierarchy must
    # not loop forever.
    boss = uuid4()
    middle = uuid4()
    events = [
        run_started(boss, 1),
        agent_created(boss, 2, role="boss"),
        child_spawned(boss, 3, child_id=middle),
        agent_created(middle, 1, role="worker", parent_id=boss),
        # Bug: middle "spawns" the boss itself, closing a cycle.
        event_row(middle, 2, "ChildSpawned", {"child_id": str(boss)}),
    ]
    # When: hierarchy metrics are computed
    metrics = compute_hierarchy(events, run_id=boss)
    # Then: the BFS terminates and reports a finite depth (1 step to
    # middle; the back-edge to boss is suppressed by the visited set).
    assert metrics.max_depth == 1
    assert metrics.num_agents == 2


def test_empty_event_list_yields_all_zero_metrics():
    # Given: no events at all
    run_id = uuid4()
    # When: hierarchy metrics are computed
    metrics = compute_hierarchy([], run_id=run_id)
    # Then: every counter is zero and the role map is empty
    assert metrics.num_agents == 0
    assert metrics.max_depth == 0
    assert metrics.max_fanout == 0
    assert metrics.agents_by_role == {}
    assert metrics.children_spawned == 0
    assert metrics.children_completed == 0
    assert metrics.children_failed == 0
