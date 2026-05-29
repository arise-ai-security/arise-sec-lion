"""Tests for per-module profiles and the SVG visualization primitives."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from experiments.shared.scripts.analysis.modularity import charts, dsm
from experiments.shared.scripts.analysis.modularity.claims import compute_module_profiles
from experiments.shared.scripts.analysis.modularity.normalize import build_run_model
from experiments.shared.scripts.db.models import EventRow


def _ev(agg: str, seq: int, etype: str, when: str, **payload) -> EventRow:
    return EventRow(f"{agg}-{seq}", agg, seq, etype, payload, when, {})


def _model():
    # builder(mgr->bw) writes /work/bin/app; exploiter(exp) reads it later.
    events = [
        _ev("boss", 1, "AgentCreated", "t0", role="boss", parent_id=None),
        _ev("boss", 2, "ChildSpawned", "t0", child_id="bld", subtask={"description": "[Builder] build"}),
        _ev("boss", 3, "ChildSpawned", "t0", child_id="exp", subtask={"description": "[Exploiter] exploit"}),
        _ev("bld", 1, "AgentCreated", "t0", role="manager", parent_id="boss", sibling_index="0"),
        _ev("bld", 2, "TaskAssigned", "t0", task_description="[Builder] build"),
        _ev("bld", 3, "ChildSpawned", "t0", child_id="bw", subtask={"description": "compile"}),
        _ev("bw", 1, "AgentCreated", "t0", role="worker", parent_id="bld", sibling_index="0"),
        _ev("exp", 1, "AgentCreated", "t0", role="worker", parent_id="boss", sibling_index="1"),
        _ev("exp", 2, "TaskAssigned", "t0", task_description="[Exploiter] exploit"),
        _ev("bw", 2, "ThoughtCaptured", "t1", output_type="tool_use", tool_name="Write",
            content='Writing: /work/bin/app\nInput: {"content": "x"}'),
        _ev("exp", 3, "ThoughtCaptured", "t2", output_type="tool_use", tool_name="Read",
            content='Reading: /work/bin/app\nInput: {"file_path": "/work/bin/app"}'),
    ]
    return build_run_model("boss", "demo", "success", events, [])


def test_instability_direction_matches_dependency():
    # builder produces (afferent), exploiter consumes (efferent) -> builder stable, exploiter unstable
    prof = compute_module_profiles(_model())
    assert prof["builder"]["afferent_ca"] >= 1
    assert prof["exploiter"]["efferent_ce"] >= 1
    assert prof["builder"]["instability"] < prof["exploiter"]["instability"]


def test_node_dsm_square_and_blocks_cover_nodes():
    nodes, labels, matrix, blocks = dsm.node_dsm(_model())
    assert len(nodes) == len(labels) == len(matrix) == sum(blocks)
    assert all(len(row) == len(nodes) for row in matrix)
    assert matrix == [list(col) for col in zip(*matrix)]  # symmetric


def test_heatmap_svg_well_formed():
    svg = dsm.render_heatmap_svg([[0.0, 2.0], [2.0, 0.0]], ["a", "b"], ["a", "b"],
                                 title="t", annotate=True, block_boundaries=[1, 1])
    ET.fromstring(svg)  # raises if malformed


def test_bar_and_histogram_svg_well_formed():
    bar = charts.grouped_bar_svg(["g1", "g2"], {"s1": [0.3, 0.6], "s2": [0.1, 0.2]},
                                 title="t", errors={"s1": [0.05, 0.04]}, ymax=1.0)
    ET.fromstring(bar)
    hist = charts.histogram_svg([-2.0, -1.5, -1.0, 0.2, 0.5], title="t", xlabel="z", vline=0.0)
    ET.fromstring(hist)
