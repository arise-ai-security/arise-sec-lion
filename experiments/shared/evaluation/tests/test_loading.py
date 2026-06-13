"""Tests for the loader internals (flatten/sort + manifest reading), no DB."""

import json
from collections import defaultdict

from experiments.shared.evaluation import loading
from experiments.shared.evaluation.tests.builders import RunBuilder


def _grouped(builder: RunBuilder) -> dict:
    grouped = defaultdict(list)
    for event in builder.events:
        grouped[event.aggregate_id].append(event)
    return dict(grouped)


def test_flatten_grouped_orders_globally_by_time() -> None:
    """Flattening interleaves aggregates back into one occurred_at-ordered stream."""
    # Given: two agents whose events were created in interleaved time order
    builder = RunBuilder()
    boss = builder.boss()
    worker = builder.agent("worker", boss, "[Builder] b")
    builder.tool(worker, ("GrepAction", "Tool: GrepAction\nInput: {}"))
    builder.completed(boss, "done")
    # When: flattening the per-aggregate grouping
    flat = loading._flatten_grouped(_grouped(builder))
    # Then: result is globally sorted by occurred_at (monotonic)
    times = [e.occurred_at for e in flat]
    assert times == sorted(times)
    assert len(flat) == len(builder.events)


def test_read_manifest_missing_returns_empty(tmp_path) -> None:
    """A missing manifest yields an empty dict, not an error."""
    # Given: a run dir with no manifest
    # When/Then
    assert loading._read_manifest(tmp_path) == {}


def test_read_manifest_corrupt_returns_empty(tmp_path) -> None:
    """A corrupt or non-object manifest yields an empty dict."""
    # Given: invalid JSON and a non-object JSON
    (tmp_path / "run_manifest.json").write_text("{not valid json")
    assert loading._read_manifest(tmp_path) == {}
    # And: a JSON array (not an object)
    (tmp_path / "run_manifest.json").write_text(json.dumps([1, 2, 3]))
    assert loading._read_manifest(tmp_path) == {}


def test_read_manifest_valid(tmp_path) -> None:
    """A valid manifest object is returned as a dict."""
    # Given
    (tmp_path / "run_manifest.json").write_text(json.dumps({"cell": "B4", "task": "x"}))
    # When/Then
    assert loading._read_manifest(tmp_path) == {"cell": "B4", "task": "x"}
