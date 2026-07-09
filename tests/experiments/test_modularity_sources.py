"""Unit tests for modularity-analysis data loading (correctness req #1/#2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.shared.scripts.analysis.modularity.sources import (
    RunIdMismatch,
    RunInfo,
    _row_from_flat,
    dedupe_by_cve,
    enumerate_runs,
    find_root_aggregate,
)
from experiments.shared.scripts.db.models import EventRow


def _run(run_id: str, task: str, status: str, started: str) -> RunInfo:
    return RunInfo(run_id=run_id, task=task, exit_status=status, started_at=started,
                   run_dir=Path("/tmp") / run_id)


def test_dedupe_prefers_success_over_failure():
    # Given: one CVE with a failed and a successful run
    runs = [_run("a", "cve-1", "failed", "2026-05-17T01:00:00Z"),
            _run("b", "cve-1", "success", "2026-05-17T03:00:00Z")]
    # When: de-duplicated
    result = dedupe_by_cve(runs)
    # Then: the success run wins; the failed run is recorded as discarded
    assert [r.run_id for r in result.chosen] == ["b"]
    assert result.discarded == {"cve-1": ["a"]}
    assert result.multi_success == []


def test_dedupe_prefers_timeout_over_failure():
    # Given: a CVE with only non-success runs (timeout vs failed)
    runs = [_run("a", "cve-1", "failed", "2026-05-17T03:00:00Z"),
            _run("b", "cve-1", "timeout", "2026-05-17T01:00:00Z")]
    # When/Then: timeout outranks failed despite being earlier
    assert [r.run_id for r in dedupe_by_cve(runs).chosen] == ["b"]


def test_dedupe_latest_started_at_breaks_ties():
    # Given: two runs with the same exit_status
    runs = [_run("old", "cve-1", "failed", "2026-05-17T01:00:00Z"),
            _run("new", "cve-1", "failed", "2026-05-17T09:00:00Z")]
    # When/Then: the most recent run wins
    assert [r.run_id for r in dedupe_by_cve(runs).chosen] == ["new"]


def test_dedupe_flags_multiple_successes():
    # Given: a CVE with two successful runs (an anomaly)
    runs = [_run("a", "cve-1", "success", "2026-05-17T01:00:00Z"),
            _run("b", "cve-1", "success", "2026-05-17T03:00:00Z")]
    # When: de-duplicated
    result = dedupe_by_cve(runs)
    # Then: the anomaly is surfaced and the latest is still chosen deterministically
    assert result.multi_success == ["cve-1"]
    assert [r.run_id for r in result.chosen] == ["b"]


def test_dedupe_distinct_cves_all_kept():
    # Given: three distinct CVEs
    runs = [_run("a", "cve-1", "success", "t"), _run("b", "cve-2", "timeout", "t"),
            _run("c", "cve-3", "failed", "t")]
    # When/Then: nothing is discarded
    result = dedupe_by_cve(runs)
    assert len(result.chosen) == 3
    assert result.discarded == {}


def test_row_from_flat_splits_meta_and_payload():
    # Given: a flat events.jsonl object (payload fields at top level)
    obj = {"event_id": "e1", "aggregate_id": "agg", "sequence_number": 4,
           "event_type": "ThoughtCaptured", "occurred_at": "2026-05-16T00:00:00Z",
           "metadata": {}, "content": "Reading: /src/x.c", "output_type": "tool_use",
           "tool_name": "Read"}
    # When: reshaped to an EventRow
    row = _row_from_flat(obj)
    # Then: meta columns map directly, everything else becomes payload
    assert row.event_type == "ThoughtCaptured"
    assert row.aggregate_id == "agg"
    assert row.sequence_number == 4
    assert row.payload == {"content": "Reading: /src/x.c", "output_type": "tool_use",
                           "tool_name": "Read"}


def test_find_root_prefers_runstarted_over_agentcreated():
    # Given: events where RunStarted and a parentless AgentCreated disagree
    events = [
        EventRow("e1", "child", 1, "AgentCreated", {"parent_id": "boss"}, "t", {}),
        EventRow("e2", "boss", 1, "AgentCreated", {"parent_id": None}, "t", {}),
        EventRow("e3", "boss", 5, "RunStarted", {"task_description": "x"}, "t", {}),
    ]
    # When/Then: RunStarted's aggregate is the authoritative root
    assert find_root_aggregate(events) == "boss"


def test_find_root_falls_back_to_parentless_agent_created():
    # Given: no RunStarted, one AgentCreated with no parent
    events = [EventRow("e1", "boss", 1, "AgentCreated", {"parent_id": None}, "t", {})]
    # When/Then: that aggregate is the root
    assert find_root_aggregate(events) == "boss"


def test_enumerate_filters_by_study_and_triangulates(tmp_path: Path):
    # Given: a runs/ tree with one matching B1 run and one foreign study
    _make_run(tmp_path, "r1", {"run_id": "r1", "study_id": "b1-batch-autogen",
                               "task": "cve-1", "exit_status": "success"})
    _make_run(tmp_path, "r2", {"run_id": "r2", "study_id": "a12-batch-autogen",
                               "task": "cve-9", "exit_status": "success"})
    # When: enumerated for B1
    runs = enumerate_runs(tmp_path, study_id="b1-batch-autogen")
    # Then: only the B1 run is returned
    assert [r.run_id for r in runs] == ["r1"]


def test_enumerate_raises_on_dir_runid_mismatch(tmp_path: Path):
    # Given: a run whose directory name disagrees with manifest run_id
    _make_run(tmp_path, "dir-x", {"run_id": "other-id", "study_id": "b1-batch-autogen",
                                  "task": "cve-1", "exit_status": "success"})
    # When/Then: triangulation fails fast
    with pytest.raises(RunIdMismatch):
        enumerate_runs(tmp_path, study_id="b1-batch-autogen")


def _make_run(root: Path, name: str, manifest: dict) -> None:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest))
