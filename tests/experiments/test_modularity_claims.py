"""Integration tests for claim metrics on a hand-built run model."""

from __future__ import annotations

from experiments.shared.scripts.analysis.modularity.claims import (
    compute_coupling,
    compute_file_overlap,
    permutation_nulls,
)
from experiments.shared.scripts.analysis.modularity.normalize import build_run_model
from experiments.shared.scripts.db.models import EventRow


def _ev(agg: str, seq: int, etype: str, when: str, **payload) -> EventRow:
    return EventRow(f"{agg}-{seq}", agg, seq, etype, payload, when, {})


def _events() -> list[EventRow]:
    # boss -> builder(mgr) -> bw(worker); boss -> exploiter(worker).
    # bw writes /work/bin/app; exploiter later reads it -> cross-module dataflow.
    return [
        _ev("boss", 1, "AgentCreated", "t0", role="boss", parent_id=None),
        _ev("boss", 2, "ChildSpawned", "t0", child_id="bld", subtask={"description": "[Builder] build"}),
        _ev("boss", 3, "ChildSpawned", "t0", child_id="exp", subtask={"description": "[Exploiter] exploit"}),
        _ev("bld", 1, "AgentCreated", "t0", role="pending", parent_id="boss", sibling_index="0"),
        _ev("bld", 2, "TaskAssigned", "t0", task_description="[Builder] build"),
        _ev("bld", 3, "ComplexityEvaluated", "t0", determined_role="manager"),
        _ev("bld", 4, "ChildSpawned", "t0", child_id="bw", subtask={"description": "compile"}),
        _ev("bw", 1, "AgentCreated", "t0", role="worker", parent_id="bld", sibling_index="0"),
        _ev("bw", 2, "TaskAssigned", "t0", task_description="compile"),
        _ev("exp", 1, "AgentCreated", "t0", role="worker", parent_id="boss", sibling_index="1"),
        _ev("exp", 2, "TaskAssigned", "t0", task_description="[Exploiter] exploit"),
        _ev("bw", 3, "ThoughtCaptured", "t1", output_type="tool_use", tool_name="Write",
            content='Writing: /work/bin/app\nInput: {"content": "x"}'),
        _ev("exp", 3, "ThoughtCaptured", "t2", output_type="tool_use", tool_name="Read",
            content='Reading: /work/bin/app\nInput: {"file_path": "/work/bin/app"}'),
    ]


def _model():
    return build_run_model("boss", "demo", "success", _events(), [])


def test_modules_assigned_across_layers():
    rm = _model()
    assert rm.module_of("bw") == "builder"
    assert rm.module_of("exp") == "exploiter"
    assert rm.module_of("boss") == "boss"


def test_cross_module_dataflow_detected_but_no_cross_module_message():
    rm = _model()
    cross = [d for d in rm.dataflows if d.is_inter_module]
    assert len(cross) == 1 and cross[0].writer == "bw" and cross[0].reader == "exp"
    coupling = compute_coupling(rm)
    # messages run only along tree edges -> never inter-module (structural)
    assert coupling["inter_module_message_count"] == 0
    assert coupling["inter_module_dataflow_count"] == 1


def test_producer_consumer_recorded_in_work_zone():
    overlap = compute_file_overlap(_model())
    assert overlap["producer_consumer_edges_by_zone"].get("work") == 1


def test_permutation_is_deterministic():
    rm = _model()
    a = permutation_nulls(rm, n_perm=50, seed=123)
    b = permutation_nulls(rm, n_perm=50, seed=123)
    assert a["dataflow_inter_fraction"]["p_value_low"] == b["dataflow_inter_fraction"]["p_value_low"]
