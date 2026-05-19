"""Hierarchy metrics computation (design doc §5)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.models import HierarchyMetrics
from experiments.shared.scripts.analysis.text.prefixes import recover_tool_name


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from experiments.shared.scripts.db.models import EventRow


_ROLES = ("boss", "manager", "worker")


@dataclass(frozen=True)
class HierarchyToolMetrics:
    """Per-role tool-call breakdown for a single boss-rooted run.

    ``role`` is decided by the depth of the emitting aggregate's
    ``AgentCreated.parent_id`` chain rooted at the boss:
        depth == 0  -> "boss"
        depth == 1  -> "manager"
        depth >= 2  -> "worker"

    Tool-use events emitted by aggregates that never received an
    ``AgentCreated`` event are bucketed under ``"unknown"`` so they
    remain visible rather than being silently dropped.
    """

    tool_calls_by_role: dict[str, int] = field(default_factory=dict)
    nodes_by_role: dict[str, int] = field(default_factory=dict)
    tool_calls_by_role_by_name: dict[str, dict[str, int]] = field(default_factory=dict)


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


def compute_hierarchy_tool_calls(
    events: Sequence[EventRow],
    *,
    run_id: UUID,
) -> HierarchyToolMetrics:
    """Group tool-use events by the emitting aggregate's role-depth bucket.

    The role-depth bucket is computed once per aggregate by walking the
    ``AgentCreated.parent_id`` chain (with cycle protection) to the run
    root. Tool-use events from aggregates that never had an
    ``AgentCreated`` recorded land in the ``"unknown"`` bucket.

    Args:
        events: Sequenced events for the run (boss subtree). Events with
            ``event_type == "ThoughtCaptured"`` and
            ``payload.output_type == "tool_use"`` are the only ones
            counted toward the tool-call totals.
        run_id: The boss aggregate id (the run root).

    Returns:
        Fully-populated ``HierarchyToolMetrics``. All three canonical
        roles are always present in ``tool_calls_by_role`` /
        ``nodes_by_role`` / ``tool_calls_by_role_by_name`` even when
        their count is zero, so downstream summarisers don't need to
        defend against missing keys.
    """
    parent_of: dict[str, str | None] = {}
    for event in events:
        if event.event_type != "AgentCreated":
            continue
        agg = str(event.aggregate_id)
        parent = event.payload.get("parent_id")
        parent_of[agg] = str(parent) if isinstance(parent, str) and parent else None

    root_str = str(run_id)
    # Ensure the root is present even if AgentCreated for it isn't first.
    parent_of.setdefault(root_str, None)

    depth_memo: dict[str, int] = {}

    def depth_of(agg: str) -> int:
        if agg in depth_memo:
            return depth_memo[agg]
        # Iterative walk with cycle detection.
        path: list[str] = []
        seen: set[str] = set()
        node: str | None = agg
        while node is not None and node not in depth_memo and node not in seen:
            seen.add(node)
            path.append(node)
            node = parent_of.get(node)
        if node is None:
            anchor_depth = -1
        elif node in depth_memo:
            anchor_depth = depth_memo[node]
        else:
            # Cycle: stop here at depth -1 so the cycle root lands at 0.
            anchor_depth = -1
        # ``path`` is ordered child-first; reversed walks root-first.
        for i, p in enumerate(reversed(path)):
            depth_memo[p] = anchor_depth + 1 + i
        return depth_memo[agg]

    def role_at(agg: str) -> str:
        if agg not in parent_of:
            return "unknown"
        d = depth_of(agg)
        if d == 0:
            return "boss"
        if d == 1:
            return "manager"
        return "worker"

    tool_calls_by_role: dict[str, int] = {r: 0 for r in _ROLES}
    tool_calls_by_role_by_name: dict[str, dict[str, int]] = {r: {} for r in _ROLES}

    for event in events:
        if event.event_type != "ThoughtCaptured":
            continue
        payload = event.payload
        if payload.get("output_type") != "tool_use":
            continue
        agg = str(event.aggregate_id)
        role = role_at(agg)
        tool_calls_by_role[role] = tool_calls_by_role.get(role, 0) + 1
        tool_name = recover_tool_name(payload.get("content") or "", payload.get("tool_name"))
        bucket = tool_calls_by_role_by_name.setdefault(role, {})
        bucket[tool_name] = bucket.get(tool_name, 0) + 1

    nodes_by_role: dict[str, int] = {r: 0 for r in _ROLES}
    for agg in parent_of:
        if agg == root_str and not parent_of[agg]:
            depth_memo.setdefault(agg, 0)
        nodes_by_role[role_at(agg)] = nodes_by_role.get(role_at(agg), 0) + 1

    return HierarchyToolMetrics(
        tool_calls_by_role=tool_calls_by_role,
        nodes_by_role=nodes_by_role,
        tool_calls_by_role_by_name=tool_calls_by_role_by_name,
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
