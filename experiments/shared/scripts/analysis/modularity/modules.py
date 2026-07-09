"""Assign each node to a module — the pluggable seam (SOLID/DIP).

The metrics pipeline depends only on the :class:`ModuleAssignment` protocol
(``node_id -> module_id``), never on how modules are defined. Two concrete
strategies exist:

  * :class:`LabeledModules` — B1: module = the prescribed ``[Builder]`` /
    ``[Exploiter]`` / ``[Fixer]`` / ``[Reporter]`` branch of a node's depth-1
    ancestor (ground truth from ``boss.j2``).
  * ``DiscoveredModules`` — A1/A2 handoff: module = a community found by
    clustering the interaction graph (added in the handoff phase).

``BOSS`` is the composition root (module ``"boss"``), not a module itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from experiments.shared.scripts.analysis.modularity.nodes import NodeTable


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


BOSS = "boss"
UNKNOWN = "unknown"
BENCHMARK_MODULES = ("builder", "exploiter", "fixer", "reporter")


def detect_branch_from_task(task_description: str) -> str | None:
    """Detect the SEC-bench branch from a subtask description.

    Verbatim port of ``plugins/security/prompt_strategy.py::
    _detect_branch_from_task`` (the system's authoritative rule): explicit
    bracket prefixes are authoritative; loose keyword matching is the
    fallback. Kept as a copy so this analysis stays decoupled from plugin
    internals; ``test_modularity_modules`` asserts parity with the source.
    """
    task_lower = task_description.lower()
    if "[builder]" in task_lower:
        return "builder"
    if "[exploiter]" in task_lower:
        return "exploiter"
    if "[fixer]" in task_lower:
        return "fixer"
    if "[reporter]" in task_lower:
        return "reporter"
    if any(kw in task_lower for kw in ("builder", "environment", "setup")):
        return "builder"
    if any(kw in task_lower for kw in ("exploiter", "exploit")):
        return "exploiter"
    if any(kw in task_lower for kw in ("fixer", "patch", "fix")):
        return "fixer"
    if any(kw in task_lower for kw in ("reporter", "security report")):
        return "reporter"
    return None


class ModuleAssignment(Protocol):
    """Maps a node id to its module id. The metrics pipeline's only contract."""

    def module_of(self, node_id: str) -> str: ...

    def modules(self) -> list[str]: ...


class LabeledModules:
    """Ground-truth module assignment from the prescribed bracket labels.

    A node's module is the detected branch of its depth-1 ancestor. The
    branch is read from that ancestor's own ``TaskAssigned`` description and
    cross-checked against the boss's ``ChildSpawned.subtask.description`` (both
    carry the bracket); a disagreement is recorded in :attr:`conflicts`.
    """

    def __init__(self, table: NodeTable, spawn_desc: dict[str, str]) -> None:
        self._table = table
        self._branch_of_root: dict[str, str] = {}
        self.conflicts: dict[str, tuple[str | None, str | None]] = {}
        for node_id, node in table.nodes.items():
            if node.parent_id != table.root_id:
                continue  # only depth-1 module roots carry a label
            from_task = detect_branch_from_task(node.task_description)
            from_spawn = detect_branch_from_task(spawn_desc.get(node_id, ""))
            if from_task and from_spawn and from_task != from_spawn:
                self.conflicts[node_id] = (from_task, from_spawn)
            self._branch_of_root[node_id] = from_task or from_spawn or UNKNOWN

    def module_of(self, node_id: str) -> str:
        if node_id == self._table.root_id:
            return BOSS
        root = self._table.module_root_of(node_id)
        if root is None:
            return UNKNOWN
        return self._branch_of_root.get(root, UNKNOWN)

    def modules(self) -> list[str]:
        return sorted({self.module_of(n) for n in self._table.nodes})

    def assign_all(self) -> dict[str, str]:
        return {n: self.module_of(n) for n in self._table.nodes}


def boss_spawn_descriptions(events: Sequence[EventRow], root_id: str) -> dict[str, str]:
    """Map each depth-1 child id to the boss's ``ChildSpawned`` subtask text."""
    out: dict[str, str] = {}
    for event in events:
        if event.event_type != "ChildSpawned" or str(event.aggregate_id) != root_id:
            continue
        child = event.payload.get("child_id")
        subtask = event.payload.get("subtask") or {}
        if child:
            out[str(child)] = subtask.get("description") or ""
    return out


def build_labeled_modules(events: Sequence[EventRow], table: NodeTable) -> LabeledModules:
    """Convenience constructor: extract boss spawn labels, build the assignment."""
    return LabeledModules(table, boss_spawn_descriptions(events, table.root_id))
