"""Regression tests for the per-study collect's projection_status filter.

Audit N-2 completion: the per-study summarize step (which drives
``summary.csv`` and ``run_metrics.csv``) must mirror
``shared.collect.build_enrollment_lock`` and exclude rows stamped
``projection_status="failed"``. Pre-fix the failed-projection run was
absent from ``enrollment.lock.yaml`` but still summed into the report
tables as a zero-event row — the two artifacts disagreed on cohort
size, which is worse than the original silent zero.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "collect.py"
_SPEC = importlib.util.spec_from_file_location("collect_test_module", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_COLLECT_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_COLLECT_MODULE)


def _stub_run(
    *,
    run_id: str,
    cell: str = "A1",
    task: str = "t1",
    projection_status: str | None = None,
    legacy_migration: bool = False,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "cell": cell,
        "task": task,
        "replicate": 0,
    }
    if projection_status is not None:
        record["projection_status"] = projection_status
    if legacy_migration:
        record["legacy_migration"] = True
    return record


def test_filtered_runs_excludes_projection_failed_rows(monkeypatch) -> None:
    # Given: load_runs returns a mix of ok, failed, and legacy-unstamped runs
    runs = [
        _stub_run(run_id="ok", projection_status="ok"),
        _stub_run(run_id="failed", projection_status="failed"),
        _stub_run(run_id="legacy-unstamped"),
    ]
    monkeypatch.setattr(_COLLECT_MODULE, "load_runs", lambda **kwargs: list(runs))

    # When
    result = _COLLECT_MODULE._filtered_runs(
        "A1", set(), exclude_legacy_migration=False
    )

    # Then: ok + legacy-unstamped survive; failed is dropped
    assert [run["run_id"] for run in result] == ["ok", "legacy-unstamped"]


def test_filtered_runs_failed_row_does_not_reach_summary(monkeypatch) -> None:
    # Given: a failed-projection run alongside one ok run. Summary aggregation
    # must NOT pick up the failed row even as a zero contribution.
    runs = [
        _stub_run(run_id="ok", projection_status="ok"),
        _stub_run(run_id="failed", projection_status="failed"),
    ]
    monkeypatch.setattr(_COLLECT_MODULE, "load_runs", lambda **kwargs: list(runs))
    # Stub out the heavy file IO: no manifests, no artifact copies, empty metrics.
    monkeypatch.setattr(_COLLECT_MODULE, "_run_manifest_index", lambda: {})
    monkeypatch.setattr(_COLLECT_MODULE, "_load_manifest", lambda: {"cells": {"A1": {}}})
    monkeypatch.setattr(_COLLECT_MODULE, "_load_dataset", lambda _m: {})
    monkeypatch.setattr(_COLLECT_MODULE, "_load_cells", lambda: ["A1"])

    # When
    rows, run_rows, _total, _inputs = _COLLECT_MODULE._summarize(["A1"])

    # Then: the cell row counts exactly one run; the per-run rows include the
    # ok run only (so the failed row never inflates the cohort).
    assert len(rows) == 1
    assert rows[0]["cell"] == "A1"
    assert rows[0]["runs"] == 1
    assert {row["run_id"] for row in run_rows} == {"ok"}
