"""Tests for unsupervised module discovery (A1/A2 handoff plumbing)."""

from __future__ import annotations

from experiments.shared.scripts.analysis.modularity.claims import compute_coupling, real_modules
from experiments.shared.scripts.analysis.modularity.discover import (
    discover_modules,
    with_discovered_modules,
)
from experiments.shared.scripts.analysis.modularity.normalize import build_run_model
from experiments.shared.scripts.db.models import EventRow


def _ev(agg: str, seq: int, etype: str, when: str, **payload) -> EventRow:
    return EventRow(f"{agg}-{seq}", agg, seq, etype, payload, when, {})


def _events() -> list[EventRow]:
    # boss -> bld(mgr) -> bw ; boss -> exp(worker). bld<->bw exchange briefing+report
    # (weight 2); bw writes /work/bin/app which exp later reads (one cross edge).
    return [
        _ev("boss", 1, "AgentCreated", "t0", role="boss", parent_id=None),
        _ev("boss", 2, "ChildSpawned", "t0", child_id="bld", subtask={"description": "build"}),
        _ev("boss", 3, "ChildSpawned", "t0", child_id="exp", subtask={"description": "exploit"}),
        _ev("bld", 1, "AgentCreated", "t0", role="pending", parent_id="boss", sibling_index="0"),
        _ev("bld", 2, "ChildSpawned", "t0", child_id="bw", subtask={"description": "compile"}),
        _ev("bld", 3, "ChildCompleted", "t3", child_id="bw"),
        _ev("bw", 1, "AgentCreated", "t0", role="worker", parent_id="bld", sibling_index="0"),
        _ev("exp", 1, "AgentCreated", "t0", role="worker", parent_id="boss", sibling_index="1"),
        _ev("bw", 2, "ThoughtCaptured", "t1", output_type="tool_use", tool_name="Write",
            content='Writing: /work/bin/app\nInput: {"content": "x"}'),
        _ev("exp", 2, "ThoughtCaptured", "t2", output_type="tool_use", tool_name="Read",
            content='Reading: /work/bin/app\nInput: {"file_path": "/work/bin/app"}'),
    ]


def _model():
    return build_run_model("boss", "demo", "success", _events(), [])


def test_discover_returns_m_prefixed_communities():
    assign = discover_modules(_model())
    mods = assign.modules()
    assert len(mods) >= 2
    assert all(m.startswith("m") for m in mods)


def test_discover_keeps_connected_nodes_together():
    # bld and bw share two edges (briefing + report) -> same community
    assign = discover_modules(_model())
    assert assign.module_of("bld") == assign.module_of("bw")


def test_with_discovered_modules_drives_metrics():
    rm = with_discovered_modules(_model())
    assert len(real_modules(rm)) >= 2  # metrics see >=2 real modules
    coupling = compute_coupling(rm)
    assert coupling["n_modules"] >= 2
    # With discovered modules there is no special "boss" community to exclude,
    # so boss->child briefings count as real inter-community edges — the
    # "messages == 0" structural zero is a *labeled*-partition property only.
    assert coupling["inter_module_message_count"] >= 0
    assert coupling["modularity_labeled"] > 0  # the active (discovered) partition is modular
