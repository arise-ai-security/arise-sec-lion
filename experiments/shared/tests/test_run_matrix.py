"""Unit tests for the matrix driver (PR 6).

These tests verify orchestration logic — job enumeration, runner dispatch,
filter handling, error capture, and post-dispatch pipeline invocation. The
runner registry is monkey-patched so no real subprocess fires.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID, uuid4

import pytest
import yaml

from experiments.shared import runners
from experiments.shared.scripts import collect, run_matrix


if TYPE_CHECKING:
    from pathlib import Path


class _FakeRunnerState(TypedDict):
    calls: list[dict[str, Any]]
    fail_for: set[tuple[str, str, int]]


# -----------------------------
# Fixtures
# -----------------------------


def _seed_study(
    repo_root: Path,
    *,
    study_id: str,
    cells: dict[str, dict[str, str]] | None = None,
    cves: list[str] | None = None,
    replicates: int = 1,
    per_cell_overrides: dict[str, dict] | None = None,
) -> Path:
    """Create a self-contained study under ``experiments/<study_id>/``."""
    cells = cells or {
        "A1": {"group": "A", "runner": "fake", "config": "configs/A1.yaml"},
        "B2": {"group": "B", "runner": "fake", "config": "configs/B2.yaml"},
    }
    cves = cves or ["cve-a", "cve-b"]

    study_dir = repo_root / "experiments" / study_id
    (study_dir / "configs").mkdir(parents=True)
    for cell_spec in cells.values():
        (repo_root / "experiments" / study_id / cell_spec["config"]).write_text("# placeholder\n")

    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "schema_version": 2,
                "cells": cells,
                "dataset": "dataset.yaml",
                "replicates": replicates,
            },
            sort_keys=False,
        )
    )

    source_paths: list[str] = []
    for cve in cves:
        filename = cve.replace(".", "-") + ".json"
        path = repo_root / "deployment" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"instance_id": cve}))
        source_paths.append(f"deployment/{filename}")

    (study_dir / "dataset.yaml").write_text(
        yaml.safe_dump(
            {
                "default_cves": cves,
                "per_cell_overrides": per_cell_overrides or {},
                "source": {"kind": "deployment-json", "paths": source_paths},
            },
            sort_keys=False,
        )
    )

    # The matrix-summary template is fixture-embedded; copy from the repo so
    # write_md can hash it. The validator never sees this path because tests
    # never call validate_reports against the tmp dir.
    template_dir = repo_root / "experiments" / "shared" / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "matrix-summary.md.j2").write_text("Total: {{ total_jobs }}\n")

    # Empty groups.yaml is fine; the validator will accept any group letter
    # so long as the cell name starts with it. We seed A and B because the
    # default fixture uses both.
    shared_dir = repo_root / "experiments" / "shared"
    (shared_dir / "groups.yaml").write_text(
        yaml.safe_dump({"A": "Group A", "B": "Group B"}, sort_keys=False)
    )

    # The validator imports from collect.py at study-validation time. The
    # tests themselves don't run validate_study but it's harmless to have
    # an empty reports dir staged.
    (study_dir / "reports").mkdir(parents=True, exist_ok=True)
    return study_dir


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> _FakeRunnerState:
    """Register a fake runner that records dispatched jobs and returns a UUID.

    The matrix driver calls ``runners.get(cell.runner)``; this fixture swaps
    the registry's lookup so the test never imports ``aris`` (which would
    drag in the real subprocess path).
    """
    state: _FakeRunnerState = {
        "calls": [],
        "fail_for": set(),  # set of (cell, task, replicate) that should raise
    }

    class _FakeRunner:
        id = "fake"
        label = "Fake"

        def run(self, **kwargs: object) -> UUID:
            entry = {
                "cell": kwargs["cell"],
                "task": kwargs["task"],
                "replicate": kwargs["replicate"],
                "config": kwargs["config"],
                "context_file": kwargs["context_file"],
                "study_id": kwargs["study_id"],
            }
            state["calls"].append(entry)
            key = (entry["cell"], entry["task"], entry["replicate"])
            if key in state["fail_for"]:
                raise RuntimeError(f"forced failure for {key}")
            return uuid4()

    instance = _FakeRunner()

    def _fake_get(runner_id: str):
        if runner_id != "fake":
            raise KeyError(f"unknown runner {runner_id!r}")
        return instance

    def _fake_all_ids() -> list[str]:
        return ["fake"]

    monkeypatch.setattr(runners, "get", _fake_get)
    monkeypatch.setattr(runners, "all_ids", _fake_all_ids)
    return state


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace post-dispatch helpers so tests don't shell out to subprocesses."""
    invocations: dict[str, list[str]] = {"order": []}

    def _fake_collect_study(study_id: str, **_kwargs: object) -> Path:
        invocations["order"].append(f"collect:{study_id}")
        return run_matrix.get_repo_root() / "fake-collect"

    def _fake_render_and_validate(study_id: str, *, repo_root: Path) -> None:  # noqa: ARG001
        invocations["order"].append(f"render:{study_id}")

    def _fake_write_matrix_summary(*args: object, **kwargs: object) -> Path:
        invocations["order"].append(f"summary:{kwargs.get('study_id') or args[0]}")
        return run_matrix.get_repo_root() / "fake-summary.md"

    monkeypatch.setattr(collect, "collect_study", _fake_collect_study)
    monkeypatch.setattr(run_matrix, "_render_and_validate", _fake_render_and_validate)
    monkeypatch.setattr(run_matrix, "_write_matrix_summary", _fake_write_matrix_summary)
    return invocations


# -----------------------------
# Job enumeration / filters
# -----------------------------


def test_run_matrix_dispatches_full_matrix_default_filters(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study with 2 cells x 2 cves x 1 replicate (4 jobs).
    _seed_study(repo_root, study_id="study-full")

    # When: we run the matrix with no filters.
    rc = run_matrix.main(["--study", "study-full"])

    # Then: every (cell, task) pair was dispatched exactly once.
    assert rc == 0
    pairs = sorted((call["cell"], call["task"], call["replicate"]) for call in fake_runner["calls"])
    assert pairs == [
        ("A1", "cve-a", 0),
        ("A1", "cve-b", 0),
        ("B2", "cve-a", 0),
        ("B2", "cve-b", 0),
    ]


def test_run_matrix_respects_cells_filter(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study with two cells.
    _seed_study(repo_root, study_id="study-cells")

    # When: we filter to cell B2 only.
    rc = run_matrix.main(["--study", "study-cells", "--cells", "B2"])

    # Then: only B2 jobs were dispatched.
    assert rc == 0
    cells_seen = {call["cell"] for call in fake_runner["calls"]}
    assert cells_seen == {"B2"}


def test_run_matrix_respects_tasks_filter(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study with two CVEs.
    _seed_study(repo_root, study_id="study-tasks")

    # When: we filter to a single task.
    rc = run_matrix.main(["--study", "study-tasks", "--tasks", "cve-a"])

    # Then: every dispatched call has that task.
    assert rc == 0
    tasks_seen = {call["task"] for call in fake_runner["calls"]}
    assert tasks_seen == {"cve-a"}


def test_run_matrix_replicates_arg_overrides_manifest(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study whose manifest declares 1 replicate.
    _seed_study(repo_root, study_id="study-rep", replicates=1)

    # When: --replicates=3 is passed.
    rc = run_matrix.main(["--study", "study-rep", "--replicates", "3"])

    # Then: each (cell, task) pair was dispatched 3 times (4 pairs * 3 = 12).
    assert rc == 0
    assert len(fake_runner["calls"]) == 12
    replicate_counts = {call["replicate"] for call in fake_runner["calls"]}
    assert replicate_counts == {0, 1, 2}


def test_run_matrix_unknown_cell_filter_raises(
    repo_root: Path,
    fake_runner: _FakeRunnerState,  # noqa: ARG001
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study with only A1 and B2.
    _seed_study(repo_root, study_id="study-bad-cell")

    # When/Then: filtering on an undeclared cell raises before dispatch.
    with pytest.raises(ValueError, match="unknown cells"):
        run_matrix.main(["--study", "study-bad-cell", "--cells", "Z9"])


# -----------------------------
# Failure handling
# -----------------------------


def test_run_matrix_continue_on_error_captures_failures(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study where one specific job is forced to fail.
    _seed_study(repo_root, study_id="study-fail")
    fake_runner["fail_for"].add(("B2", "cve-b", 0))

    # When: run with default --continue-on-error.
    rc = run_matrix.main(["--study", "study-fail"])

    # Then: rc=1 (any failure) but every job was attempted.
    assert rc == 1
    assert len(fake_runner["calls"]) == 4


def test_run_matrix_continue_on_error_false_raises_on_first_failure(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a forced failure on the first dispatched job.
    _seed_study(repo_root, study_id="study-strict-fail")
    fake_runner["fail_for"].add(("A1", "cve-a", 0))

    # When/Then: --no-continue-on-error propagates the runner exception.
    with pytest.raises(RuntimeError, match="forced failure"):
        run_matrix.main(["--study", "study-strict-fail", "--no-continue-on-error"])


def test_run_matrix_returns_zero_on_all_success(
    repo_root: Path,
    fake_runner: _FakeRunnerState,  # noqa: ARG001
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: no failures injected.
    _seed_study(repo_root, study_id="study-ok")

    # When: matrix runs to completion.
    rc = run_matrix.main(["--study", "study-ok"])

    # Then: exit code is 0.
    assert rc == 0


def test_run_matrix_dry_run_enumerates_without_dispatch(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],
) -> None:
    # Given: a study with real jobs to enumerate.
    _seed_study(repo_root, study_id="study-dry")

    # When: --dry-run is passed.
    rc = run_matrix.main(["--study", "study-dry", "--cells", "A1", "--dry-run"])

    # Then: validation/enumeration succeeded but no runner or report pipeline fired.
    assert rc == 0
    assert fake_runner["calls"] == []
    assert stub_pipeline["order"] == []


def test_run_matrix_returns_one_on_any_failure(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: one forced failure.
    _seed_study(repo_root, study_id="study-onefail")
    fake_runner["fail_for"].add(("A1", "cve-a", 0))

    # When: run with default flags.
    rc = run_matrix.main(["--study", "study-onefail"])

    # Then: exit code is 1.
    assert rc == 1


# -----------------------------
# Pipeline orchestration
# -----------------------------


def test_run_matrix_validates_manifest_before_dispatch(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    stub_pipeline: dict[str, list[str]],  # noqa: ARG001
) -> None:
    # Given: a study whose manifest references a missing config file.
    study_id = "study-bad-manifest"
    _seed_study(repo_root, study_id=study_id)
    # Delete a config file referenced by the manifest so validate_manifest fails.
    (repo_root / "experiments" / study_id / "configs" / "A1.yaml").unlink()

    # When/Then: validation fails before any runner is dispatched.
    with pytest.raises(ValueError, match="manifest validation failed"):
        run_matrix.main(["--study", study_id])
    assert fake_runner["calls"] == []


def test_run_matrix_invokes_collect_render_validate_in_order(
    repo_root: Path,
    fake_runner: _FakeRunnerState,  # noqa: ARG001
    stub_pipeline: dict[str, list[str]],
) -> None:
    # Given: a clean study.
    _seed_study(repo_root, study_id="study-pipeline")

    # When: matrix runs.
    rc = run_matrix.main(["--study", "study-pipeline"])

    # Then: post-dispatch pipeline ran in collect → render → summary order.
    assert rc == 0
    assert stub_pipeline["order"] == [
        "collect:study-pipeline",
        "render:study-pipeline",
        "summary:study-pipeline",
    ]


def test_run_matrix_no_render_skips_render_step(
    repo_root: Path,
    fake_runner: _FakeRunnerState,  # noqa: ARG001
    stub_pipeline: dict[str, list[str]],
) -> None:
    # Given: a clean study.
    _seed_study(repo_root, study_id="study-norender")

    # When: --no-render is passed.
    rc = run_matrix.main(["--study", "study-norender", "--no-render"])

    # Then: collect and summary still run, render is skipped.
    assert rc == 0
    assert "render:study-norender" not in stub_pipeline["order"]
    assert "collect:study-norender" in stub_pipeline["order"]
    assert "summary:study-norender" in stub_pipeline["order"]


# -----------------------------
# Parallel dispatch
# -----------------------------


def test_run_matrix_parallel_dispatches_concurrently(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--parallel >1 must invoke multiple workers concurrently.

    We patch the runner registry directly so the fake's run() blocks on a
    barrier; if dispatch were sequential the barrier would deadlock.
    """
    _seed_study(repo_root, study_id="study-par")

    barrier = threading.Barrier(2, timeout=5.0)

    class _BarrierRunner:
        id = "fake"
        label = "fake"

        def run(self, **_kwargs: object) -> UUID:
            barrier.wait()  # deadlocks if dispatch isn't concurrent
            return uuid4()

    instance = _BarrierRunner()
    monkeypatch.setattr(runners, "get", lambda rid: instance if rid == "fake" else None)
    monkeypatch.setattr(runners, "all_ids", lambda: ["fake"])

    # Stub the post-dispatch pipeline (we only care about concurrent dispatch).
    monkeypatch.setattr(collect, "collect_study", lambda *_a, **_k: None)
    monkeypatch.setattr(run_matrix, "_render_and_validate", lambda *_a, **_k: None)
    monkeypatch.setattr(run_matrix, "_write_matrix_summary", lambda *_a, **_k: None)

    # Cells filtered to two so the barrier can proceed once two dispatches enter.
    rc = run_matrix.main(
        [
            "--study",
            "study-par",
            "--cells",
            "A1,B2",
            "--tasks",
            "cve-a",
            "--parallel",
            "2",
        ]
    )
    assert rc == 0


# -----------------------------
# Matrix summary output
# -----------------------------


def test_run_matrix_writes_matrix_summary(
    repo_root: Path,
    fake_runner: _FakeRunnerState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a study where one job is forced to fail so the summary has both
    # success and failure rows.
    _seed_study(repo_root, study_id="study-summary")
    fake_runner["fail_for"].add(("B2", "cve-b", 0))

    # Stub collect/render so the test only exercises the summary writer.
    monkeypatch.setattr(collect, "collect_study", lambda *_a, **_k: None)
    monkeypatch.setattr(run_matrix, "_render_and_validate", lambda *_a, **_k: None)

    # When: matrix runs.
    rc = run_matrix.main(["--study", "study-summary"])

    # Then: matrix-summary.md exists under reports/ and contains the expected
    # sections with succeeded/failed counts and the failure detail.
    assert rc == 1
    summary_path = repo_root / "experiments" / "study-summary" / "reports" / "matrix-summary.md"
    assert summary_path.is_file()
    body = summary_path.read_text(encoding="utf-8")
    assert "Total: 4" in body  # template-rendered total job count
