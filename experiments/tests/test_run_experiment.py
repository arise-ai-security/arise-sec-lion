"""Tests for the experiment runner orchestration logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from experiments.run_experiment import (
    RunPlan,
    _compute_cost_breakdown,
    _normalize_sanitizer_error,
    _validate_cve_data,
    is_resumable,
    plan_runs,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_plan_runs_generates_cartesian_product_with_anchor_replicates() -> None:
    """plan_runs creates len(cves) * len(cells) base plans + (anchor_replicates - 1) extras."""
    # Given: 3 CVE instances, 6 cells, anchor_replicates=3
    cves = ["njs.cve-1", "openjpeg.cve-2", "faad2.cve-3"]

    # When: plan_runs generates the schedule
    plans = plan_runs(
        cves=cves,
        cells=["A1", "A2", "A3", "A4", "B1", "B2"],
        anchor_cve="openjpeg.cve-2",
        anchor_cell="A1",
        anchor_replicates=3,
        seed=42,
    )

    # Then: 3 cves * 6 cells + 2 extra anchor replicates = 20
    assert len(plans) == 3 * 6 + 2


def test_plan_runs_assignment_is_deterministic_for_same_seed() -> None:
    """Same seed produces identical schedule (RunPlan list equality)."""
    # Given/When: two plans with the same seed
    plans_a = plan_runs(
        cves=["a", "b"],
        cells=["A1", "B1"],
        anchor_cve="a",
        anchor_cell="A1",
        anchor_replicates=1,
        seed=1,
    )
    plans_b = plan_runs(
        cves=["a", "b"],
        cells=["A1", "B1"],
        anchor_cve="a",
        anchor_cell="A1",
        anchor_replicates=1,
        seed=1,
    )
    # Then: identical
    assert plans_a == plans_b


def _write_clean_events(run_dir: Path) -> None:
    """Write a minimal events.jsonl that ends with run_completed."""
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        '{"event_type": "run_started", "occurred_at": "2026-04-19T12:00:00+00:00"}',
        '{"event_type": "run_completed", "occurred_at": "2026-04-19T12:00:01+00:00"}',
    ]
    (run_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")


def test_is_resumable_returns_true_when_run_completed_and_no_anomaly(tmp_path: Path) -> None:
    """is_resumable=True only when mechanical.json exists, events.jsonl ends in run_completed, and no anomaly.json."""

    # Given: a run dir with mechanical.json and a run_completed event
    run_dir = tmp_path / "njs.cve-1" / "A1" / "0"
    _write_clean_events(run_dir)
    (run_dir / "mechanical.json").write_text("{}")

    plan = RunPlan(cve_id="njs.cve-1", cell="A1", replicate=0, run_dir=run_dir)
    # When/Then: is_resumable returns True
    assert is_resumable(plan) is True


def test_is_resumable_returns_false_when_only_meta_exists(tmp_path: Path) -> None:
    """is_resumable=False when mechanical.json is missing (run never completed)."""

    # Given: a run dir with only meta.json (no mechanical.json, no events.jsonl)
    run_dir = tmp_path / "njs.cve-1" / "A1" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text("{}")
    plan = RunPlan(cve_id="njs.cve-1", cell="A1", replicate=0, run_dir=run_dir)
    # When/Then: is_resumable returns False
    assert is_resumable(plan) is False


def test_is_resumable_returns_false_when_anomaly_flagged(tmp_path: Path) -> None:
    """An anomaly-flagged run re-executes on resume even when mechanical.json is present."""

    # Given: a run dir with mechanical.json, a clean events.jsonl, AND anomaly.json
    run_dir = tmp_path / "njs.cve-1" / "B2" / "0"
    _write_clean_events(run_dir)
    (run_dir / "mechanical.json").write_text("{}")
    (run_dir / "anomaly.json").write_text('[{"kind": "malformed_json_after_retries"}]')

    plan = RunPlan(cve_id="njs.cve-1", cell="B2", replicate=0, run_dir=run_dir)
    # When/Then: is_resumable returns False so the operator can re-run after fixing
    assert is_resumable(plan) is False


def _write_events(run_dir: Path, events: list[dict]) -> Path:
    import json

    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return events_path


def test_compute_cost_breakdown_sums_both_event_types(tmp_path: Path) -> None:
    """_compute_cost_breakdown sums tokens_consumed + worker_cost_recorded into total_usd.

    Pre-Step-6 behaviour summed only tokens_consumed, which silently
    dropped tree-worker cost; this test pins the fix.
    """

    # Given: events.jsonl with both a tokens_consumed and two worker_cost_recorded rows
    run_dir = tmp_path / "mixed-cost"
    events = [
        {"event_type": "run_started", "payload": {}},
        {"event_type": "tokens_consumed", "payload": {"cost_usd": 0.47}},
        {"event_type": "worker_cost_recorded", "payload": {"cost_usd": 3.90}},
        {"event_type": "worker_cost_recorded", "payload": {"cost_usd": 9.71}},
        {"event_type": "run_completed", "payload": {}},
    ]
    _write_events(run_dir, events)
    plan = RunPlan(cve_id="njs.cve-1", cell="B1", replicate=0, run_dir=run_dir)

    # When: compute the breakdown
    breakdown = _compute_cost_breakdown(plan)

    # Then: each stream is reported independently and total is the sum
    assert breakdown["tokens_consumed_usd"] == pytest.approx(0.47)
    assert breakdown["worker_recorded_usd"] == pytest.approx(13.61)
    assert breakdown["total_usd"] == pytest.approx(14.08)


def test_compute_cost_breakdown_returns_zeros_when_no_events_file(tmp_path: Path) -> None:
    """A run with no events.jsonl yields a zero-valued breakdown (not a KeyError)."""

    # Given: a run dir with no events.jsonl
    run_dir = tmp_path / "empty"
    run_dir.mkdir(parents=True)
    plan = RunPlan(cve_id="njs.cve-1", cell="A1", replicate=0, run_dir=run_dir)

    # When: compute the breakdown
    breakdown = _compute_cost_breakdown(plan)

    # Then: all three keys present and zero
    assert breakdown == {
        "tokens_consumed_usd": 0.0,
        "worker_recorded_usd": 0.0,
        "total_usd": 0.0,
    }


def test_compute_cost_breakdown_ignores_events_without_cost_usd(tmp_path: Path) -> None:
    """Events without cost_usd are silently skipped (tool_use, run_started, etc.)."""

    # Given: events that carry no cost_usd and one that does
    run_dir = tmp_path / "noise"
    events = [
        {"event_type": "run_started", "payload": {}},
        {"event_type": "tool_use", "payload": {"tool_name": "Bash"}},
        {"event_type": "tokens_consumed", "payload": {"cost_usd": 1.25}},
        {"event_type": "run_completed", "payload": {}},
    ]
    _write_events(run_dir, events)
    plan = RunPlan(cve_id="njs.cve-1", cell="A1", replicate=0, run_dir=run_dir)

    # When: compute the breakdown
    breakdown = _compute_cost_breakdown(plan)

    # Then: only the cost-bearing row contributed
    assert breakdown["tokens_consumed_usd"] == pytest.approx(1.25)
    assert breakdown["worker_recorded_usd"] == 0.0
    assert breakdown["total_usd"] == pytest.approx(1.25)


def test_is_resumable_returns_false_when_mechanical_exists_but_events_truncated(
    tmp_path: Path,
) -> None:
    """Crash between mechanical.json write and anomaly.json write must not mark run resumable.

    This covers the narrow crash window: the runner wrote mechanical.json but
    the process died before detect_anomalies ran. Without the run_completed
    gate, the next resume would silently skip this broken row.
    """

    # Given: mechanical.json present but events.jsonl does not contain run_completed
    run_dir = tmp_path / "njs.cve-1" / "B2" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "mechanical.json").write_text("{}")
    # Events without run_completed (simulating process-kill after mechanical write)
    (run_dir / "events.jsonl").write_text(
        '{"event_type": "run_started", "occurred_at": "2026-04-19T12:00:00+00:00"}\n',
        encoding="utf-8",
    )

    plan = RunPlan(cve_id="njs.cve-1", cell="B2", replicate=0, run_dir=run_dir)
    # When/Then: is_resumable returns False so the run is re-executed
    assert is_resumable(plan) is False


def test_normalize_sanitizer_error_extracts_bare_class_from_framed() -> None:
    """_normalize_sanitizer_error pulls the bare class out of a CVEInstance framed string."""
    # Given: a framed CVEInstance-style string
    framed = "ERROR: AddressSanitizer: heap-buffer-overflow"
    # When/Then: the bare class is extracted
    assert _normalize_sanitizer_error(framed) == "heap-buffer-overflow"


def test_normalize_sanitizer_error_raises_on_unknown_class() -> None:
    """_normalize_sanitizer_error raises ValueError when no known class is present."""
    # Given: a string with no known sanitizer class
    unknown = "ERROR: MemorySanitizer"
    # When/Then: ValueError is raised
    with pytest.raises(ValueError, match="Could not extract"):
        _normalize_sanitizer_error(unknown)


def test_plan_runs_handles_anchor_replicates_zero() -> None:
    """anchor_replicates=1 yields no extra replicate entries beyond the base schedule."""
    # Given: anchor_replicates=1 (only the implicit replicate 0)
    plans = plan_runs(
        cves=["a"],
        cells=["A1", "A2"],
        anchor_cve="a",
        anchor_cell="A1",
        anchor_replicates=1,
        seed=0,
    )
    # When/Then: exactly 1 cve * 2 cells = 2 base plans, no extras
    assert len(plans) == 2


def test_plan_runs_run_dir_absolute_when_output_root_absolute(tmp_path: Path) -> None:
    """plan_runs respects an absolute output_root and produces absolute run_dir paths."""
    # Given: an absolute output root
    abs_root = (tmp_path / "runs").resolve()
    # When: plans created
    plans = plan_runs(
        cves=["a"],
        cells=["A1"],
        anchor_cve="a",
        anchor_cell="A1",
        anchor_replicates=1,
        seed=1,
        output_root=abs_root,
    )
    # Then: every run_dir is absolute (so Docker bind-mounts work)
    assert all(p.run_dir.is_absolute() for p in plans)


def test_plan_runs_raises_when_anchor_cell_not_in_cells() -> None:
    """plan_runs raises ValueError if anchor_replicates > 1 and anchor_cell is unknown."""
    # Given: anchor_replicates=3 but anchor_cell not in cells
    # When/Then: ValueError raised before any plans are constructed
    with pytest.raises(ValueError, match="anchor_cell"):
        plan_runs(
            cves=["a"],
            cells=["A1"],
            anchor_cve="a",
            anchor_cell="B99",
            anchor_replicates=3,
            seed=1,
        )


def test_plan_runs_raises_when_anchor_cve_not_in_cves() -> None:
    """plan_runs raises ValueError if anchor_replicates > 1 and anchor_cve is unknown."""
    # Given: anchor_replicates=3 but anchor_cve not in cves
    # When/Then: ValueError raised before any plans are constructed
    with pytest.raises(ValueError, match="anchor_cve"):
        plan_runs(
            cves=["a"],
            cells=["A1"],
            anchor_cve="unknown-cve",
            anchor_cell="A1",
            anchor_replicates=3,
            seed=1,
        )


def test_validate_cve_data_raises_on_missing_keys() -> None:
    """_validate_cve_data raises ValueError listing the missing required keys."""
    # Given: an entry missing docker_image and base_commit
    cve_data = {
        "json_path": "/fake/path/x.json",
        "task_text": "t",
        "expected_sanitizer_error": "ERROR: AddressSanitizer: heap-buffer-overflow",
    }
    # When/Then: ValueError enumerates the missing keys
    with pytest.raises(ValueError, match="missing required keys"):
        _validate_cve_data(cve_data, "some-cve")
