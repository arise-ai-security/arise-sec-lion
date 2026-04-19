"""Tests for the experiment runner orchestration logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from experiments.run_experiment import (
    RunPlan,
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


def test_is_resumable_returns_true_when_events_file_exists(tmp_path: Path) -> None:
    """is_resumable=True when events.jsonl exists in the run dir."""
    # Given: a run dir with events.jsonl
    run_dir = tmp_path / "njs.cve-1" / "A1" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("")

    plan = RunPlan(cve_id="njs.cve-1", cell="A1", replicate=0, run_dir=run_dir)
    # When/Then: is_resumable returns True
    assert is_resumable(plan) is True


def test_is_resumable_returns_false_when_only_meta_exists(tmp_path: Path) -> None:
    """is_resumable=False when events.jsonl is missing (only meta.json present)."""
    # Given: a run dir with only meta.json
    run_dir = tmp_path / "njs.cve-1" / "A1" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text("{}")
    plan = RunPlan(cve_id="njs.cve-1", cell="A1", replicate=0, run_dir=run_dir)
    # When/Then: is_resumable returns False
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
