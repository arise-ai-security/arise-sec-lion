"""Assemble one run's normalized model: nodes, messages, dataflow, global ctx.

This is the intermediate representation every metric consumes. The key derived
relation is **producer->consumer dataflow**: if node A writes path *p* and node
B later reads *p* (B != A), that is a directed data dependency A->B — the
genuinely behavioral coupling signal (persisted briefing/report messages can
only run along tree edges, so they never cross module boundaries except via the
boss). Cross-aggregate ordering uses ``occurred_at`` (coarse; see design §8.4).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.modularity.modules import BOSS, build_labeled_modules
from experiments.shared.scripts.analysis.modularity.nodes import build_node_table
from experiments.shared.scripts.analysis.modularity.paths import Touch, extract_touches


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.analysis.modularity.modules import LabeledModules
    from experiments.shared.scripts.analysis.modularity.nodes import NodeTable
    from experiments.shared.scripts.db.models import EventRow


@dataclass(frozen=True)
class Message:
    """A persisted context-passing edge (briefing down / report up)."""

    src: str
    dst: str
    channel: str  # briefing | report
    src_module: str
    dst_module: str

    @property
    def is_inter_module(self) -> bool:
        return _inter_module(self.src_module, self.dst_module)


@dataclass(frozen=True)
class DataFlow:
    """A producer->consumer file dependency: ``writer`` wrote ``path``, ``reader`` later read it."""

    writer: str
    reader: str
    path: str
    zone: str | None
    writer_module: str
    reader_module: str

    @property
    def is_inter_module(self) -> bool:
        return _inter_module(self.writer_module, self.reader_module)


@dataclass(frozen=True)
class GlobalWrite:
    """A write to the shared store ("global context"), attributed to its author."""

    kind: str  # artifact | decision
    key: str
    author: str
    author_module: str
    seq: int


@dataclass
class RunModel:
    run_id: str
    task: str
    exit_status: str
    table: NodeTable
    assign: LabeledModules
    touches: list[Touch]
    messages: list[Message]
    dataflows: list[DataFlow]
    global_writes: list[GlobalWrite]

    def module_of(self, node_id: str) -> str:
        return self.assign.module_of(node_id)


def _inter_module(a: str, b: str) -> bool:
    """True when two modules differ and neither side is the composition root."""
    return a != b and BOSS not in (a, b)


def build_messages(
    events: Sequence[EventRow], table: NodeTable, assign: LabeledModules
) -> list[Message]:
    messages: list[Message] = []
    for event in events:
        agg = str(event.aggregate_id)
        if event.event_type == "ChildSpawned":
            child = event.payload.get("child_id")
            if child:
                child = str(child)
                messages.append(Message(agg, child, "briefing",
                                        assign.module_of(agg), assign.module_of(child)))
        elif event.event_type in ("ChildCompleted", "ChildFailed"):
            child = event.payload.get("child_id")
            if child:
                child = str(child)
                messages.append(Message(child, agg, "report",
                                        assign.module_of(child), assign.module_of(agg)))
    return messages


def build_dataflows(touches: Sequence[Touch], assign: LabeledModules) -> list[DataFlow]:
    """Producer->consumer edges via last-writer-before-read per path.

    A read/search of ``p`` by B is linked to the most recent *prior* write/edit
    of ``p`` by some A != B. Self-reads (A == B) are intra-node and ignored.
    """
    by_path: dict[str, list[Touch]] = collections.defaultdict(list)
    for touch in touches:
        if touch.path and touch.op in ("write", "edit", "read", "search"):
            by_path[touch.path].append(touch)

    flows: list[DataFlow] = []
    for path, items in by_path.items():
        items.sort(key=lambda t: (t.occurred_at, t.seq))
        last_writer: str | None = None
        for touch in items:
            if touch.op in ("write", "edit"):
                last_writer = touch.node_id
            elif last_writer is not None and last_writer != touch.node_id:
                flows.append(DataFlow(last_writer, touch.node_id, path, touch.zone,
                                      assign.module_of(last_writer), assign.module_of(touch.node_id)))
    return flows


def build_global_writes(
    shared_events: Sequence[EventRow], assign: LabeledModules
) -> list[GlobalWrite]:
    writes: list[GlobalWrite] = []
    for event in shared_events:
        payload = event.payload
        if event.event_type == "ArtifactStored":
            author = str(payload.get("stored_by") or "")
            writes.append(GlobalWrite("artifact", payload.get("key") or "", author,
                                      assign.module_of(author), event.sequence_number))
        elif event.event_type == "DecisionRecorded":
            author = str(payload.get("decided_by") or "")
            writes.append(GlobalWrite("decision", payload.get("decision_key") or "", author,
                                      assign.module_of(author), event.sequence_number))
    return writes


def build_run_model(
    run_id: str,
    task: str,
    exit_status: str,
    events: Sequence[EventRow],
    shared_events: Sequence[EventRow],
) -> RunModel:
    """Assemble the full normalized model for one run."""
    table = build_node_table(events, run_id)
    assign = build_labeled_modules(events, table)
    touches = [touch for event in events for touch in extract_touches(event)]
    return RunModel(
        run_id=run_id,
        task=task,
        exit_status=exit_status,
        table=table,
        assign=assign,
        touches=touches,
        messages=build_messages(events, table, assign),
        dataflows=build_dataflows(touches, assign),
        global_writes=build_global_writes(shared_events, assign),
    )
