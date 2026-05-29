"""Reconstruct the agent tree (nodes, parent links, depth) from events.

A node's identity is its ``aggregate_id``; the first ``AgentCreated`` on that
aggregate carries ``parent_id`` and ``sibling_index``. Depth is derived by
walking ``parent_id`` to the root (``run_id``). Role is the depth-derived
view used elsewhere in the codebase, but we also keep the event-declared role
(``ComplexityEvaluated.determined_role`` overrides ``AgentCreated.role``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


def _to_int(value: object) -> int | None:
    """Coerce a sibling index that may be int (DB) or str (jsonl)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Node:
    node_id: str
    parent_id: str | None
    depth: int  # 0 = boss/root, 1 = module root, >=2 = deeper worker
    sibling_index: int | None
    role_declared: str  # boss/manager/worker/pending (post ComplexityEvaluated)
    task_description: str
    success_criteria: str
    n_children: int

    @property
    def is_leaf(self) -> bool:
        return self.n_children == 0


@dataclass
class NodeTable:
    root_id: str
    nodes: dict[str, Node]
    children: dict[str, list[str]] = field(default_factory=dict)

    def depth_of(self, node_id: str) -> int:
        node = self.nodes.get(node_id)
        return node.depth if node else -1

    def module_root_of(self, node_id: str) -> str | None:
        """The depth-1 ancestor (direct child of the boss) of ``node_id``.

        Returns ``None`` for the root itself or for a node detached from the
        root (no ``parent_id`` chain reaching the boss).
        """
        if node_id == self.root_id:
            return None
        seen: set[str] = set()
        current = node_id
        while True:
            parent = self.nodes[current].parent_id if current in self.nodes else None
            if parent is None:
                return None
            if parent == self.root_id:
                return current
            if current in seen:
                return None
            seen.add(current)
            current = parent


def build_node_table(events: Sequence[EventRow], root_id: str) -> NodeTable:
    """Build the node table for one run from its (subtree) events."""
    created: dict[str, EventRow] = {}
    assigned: dict[str, str] = {}
    determined_role: dict[str, str] = {}
    children: dict[str, list[str]] = {}

    for event in events:
        agg = str(event.aggregate_id)
        if event.event_type == "AgentCreated":
            created.setdefault(agg, event)
        elif event.event_type == "TaskAssigned":
            assigned.setdefault(agg, event.payload.get("task_description") or "")
        elif event.event_type == "ComplexityEvaluated":
            role = event.payload.get("determined_role")
            if role:
                determined_role[agg] = role
        elif event.event_type == "ChildSpawned":
            child = event.payload.get("child_id")
            if child:
                children.setdefault(agg, []).append(str(child))

    parent_of: dict[str, str | None] = {}
    for agg, event in created.items():
        parent = event.payload.get("parent_id")
        parent_of[agg] = str(parent) if parent else None
    parent_of.setdefault(root_id, None)

    depth_memo: dict[str, int] = {}

    def depth_of(agg: str) -> int:
        if agg in depth_memo:
            return depth_memo[agg]
        steps = 0
        current = agg
        seen: set[str] = set()
        while parent_of.get(current) is not None:
            if current in seen or steps > 64:  # cycle / runaway guard
                break
            seen.add(current)
            current = parent_of[current]  # type: ignore[assignment]
            steps += 1
        depth_memo[agg] = steps
        return steps

    nodes: dict[str, Node] = {}
    for agg, event in created.items():
        payload = event.payload
        nodes[agg] = Node(
            node_id=agg,
            parent_id=parent_of.get(agg),
            depth=depth_of(agg),
            sibling_index=_to_int(payload.get("sibling_index")),
            role_declared=determined_role.get(agg, payload.get("role") or ""),
            task_description=assigned.get(agg, ""),
            success_criteria=payload.get("success_criteria") or "",
            n_children=len(children.get(agg, [])),
        )

    return NodeTable(root_id=root_id, nodes=nodes, children=children)
