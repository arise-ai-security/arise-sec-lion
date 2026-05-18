"""Hierarchy metrics computation (design doc §5)."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.models import HierarchyMetrics


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from experiments.shared.scripts.db.models import EventRow


def compute_hierarchy(
    events: Sequence[EventRow],
    *,
    run_id: UUID,
) -> HierarchyMetrics:
    """Compute hierarchy metrics for a single boss-rooted run.

    For flat-mode A1/A2 runs there is only one ``AgentCreated`` on the boss
    aggregate and no ``ChildSpawned``; the metrics degrade to ``num_agents=1``,
    ``max_depth=0``, ``max_fanout=0``, ``agents_by_role={"boss": 1}`` and zero
    child counters.

    Args:
        events: Sequenced events for the run, in arrival order.
        run_id: The boss aggregate id used as the BFS root for depth.

    Returns:
        A fully-populated ``HierarchyMetrics`` instance.
    """
    num_agents = 0
    agents_by_role: dict[str, int] = defaultdict(int)
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    children_spawned = 0
    children_completed = 0
    children_failed = 0

    for event in events:
        event_type = event.event_type
        if event_type == "AgentCreated":
            num_agents += 1
            role = event.payload.get("role")
            if isinstance(role, str):
                agents_by_role[role] += 1
        elif event_type == "ChildSpawned":
            children_spawned += 1
            child_id = event.payload.get("child_id")
            if isinstance(child_id, str):
                children_by_parent[str(event.aggregate_id)].append(child_id)
        elif event_type == "ChildCompleted":
            children_completed += 1
        elif event_type == "ChildFailed":
            children_failed += 1

    max_fanout = max(
        (len(children) for children in children_by_parent.values()),
        default=0,
    )
    max_depth = _bfs_max_depth(children_by_parent, root=str(run_id))

    return HierarchyMetrics(
        num_agents=num_agents,
        max_depth=max_depth,
        max_fanout=max_fanout,
        agents_by_role=dict(agents_by_role),
        children_spawned=children_spawned,
        children_completed=children_completed,
        children_failed=children_failed,
    )


def _bfs_max_depth(
    children_by_parent: dict[str, list[str]],
    *,
    root: str,
) -> int:
    """Return the depth of the deepest descendant reachable from ``root``.

    Depth of ``root`` itself is 0. Cycles introduced by malformed event
    histories are tolerated via a visited set; the function always
    terminates and returns a finite integer.
    """
    if root not in children_by_parent:
        return 0

    visited: set[str] = {root}
    frontier: list[tuple[str, int]] = [(root, 0)]
    deepest = 0
    while frontier:
        node, depth = frontier.pop()
        deepest = max(deepest, depth)
        for child in children_by_parent.get(node, ()):
            if child in visited:
                continue
            visited.add(child)
            frontier.append((child, depth + 1))
    return deepest
