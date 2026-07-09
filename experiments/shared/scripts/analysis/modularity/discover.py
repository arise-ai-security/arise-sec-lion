"""A1/A2 handoff: discover modules as interaction-graph communities.

For studies WITHOUT prescribed module labels — A1 (ad-hoc sub-agents) and A2
(a single flat agent) — a "module" is an *emergent community* in the
interaction + producer/consumer-dataflow graph, found by the same modularity
maximization validated on B1. It plugs in behind the ``ModuleAssignment`` seam,
so every Claim metric runs unchanged.

Validation strategy: on B1 (the only labeled data here) the discovered
communities recover the prescribed Builder/Exploiter/Fixer/Reporter partition
(high NMI/ARI), which licenses trusting discovery where no labels exist.

NOTE: not exercised on real A1/A2 data in this repo (none present). For the A2
monolith (a single node, no tree) community detection is degenerate; that case
needs temporal/activity segmentation of the action stream — see HANDOFF notes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.modularity.claims import interaction_weights
from experiments.shared.scripts.analysis.modularity.graph import greedy_communities
from experiments.shared.scripts.analysis.modularity.normalize import DataFlow, GlobalWrite, Message

if TYPE_CHECKING:
    from experiments.shared.scripts.analysis.modularity.normalize import RunModel


class DiscoveredModules:
    """ModuleAssignment whose modules are detected communities (``m0``, ``m1`` …)."""

    def __init__(self, communities: dict[str, int]) -> None:
        self._communities = dict(communities)

    def module_of(self, node_id: str) -> str:
        community = self._communities.get(node_id)
        return f"m{community}" if community is not None else "unknown"

    def modules(self) -> list[str]:
        return sorted({self.module_of(n) for n in self._communities})

    def assign_all(self) -> dict[str, str]:
        return {n: self.module_of(n) for n in self._communities}


def discover_modules(rm: RunModel) -> DiscoveredModules:
    """Cluster the run's interaction graph into emergent module communities."""
    weights = interaction_weights(rm)
    return DiscoveredModules(greedy_communities(list(rm.table.nodes), weights))


def with_discovered_modules(rm: RunModel) -> RunModel:
    """Return a copy of ``rm`` re-annotated with discovered-community modules."""
    assign = discover_modules(rm)
    messages = [Message(m.src, m.dst, m.channel, assign.module_of(m.src), assign.module_of(m.dst))
                for m in rm.messages]
    dataflows = [DataFlow(d.writer, d.reader, d.path, d.zone,
                          assign.module_of(d.writer), assign.module_of(d.reader))
                 for d in rm.dataflows]
    global_writes = [GlobalWrite(g.kind, g.key, g.author, assign.module_of(g.author), g.seq)
                     for g in rm.global_writes]
    return replace(rm, assign=assign, messages=messages, dataflows=dataflows,
                   global_writes=global_writes)
