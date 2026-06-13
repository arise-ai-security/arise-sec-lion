"""Tests for topology detection."""

import json
from uuid import uuid4

import pytest

from experiments.shared.evaluation import loading
from experiments.shared.evaluation.tests.builders import RunBuilder


def test_topology_of_structural(tmp_path) -> None:
    """A single-agent run is linear; a run that spawned children is BEF."""
    # Given: linear (one aggregate)
    linear = RunBuilder()
    linear.boss()
    # And: hierarchical (boss + child)
    hierarchical = RunBuilder()
    boss = hierarchical.boss()
    hierarchical.agent("worker", boss, "[Builder] x")
    # Then
    assert loading.topology_of(linear.run_data(tmp_path)) == "linear"
    assert loading.topology_of(hierarchical.run_data(tmp_path)) == "bef"


def test_detect_topology_from_manifest(tmp_path) -> None:
    """Manifest cell/study_id decides bef vs linear without touching the DB."""
    # Given: an N1 and a B4 manifest on disk
    n_id = uuid4()
    (tmp_path / str(n_id)).mkdir()
    (tmp_path / str(n_id) / "run_manifest.json").write_text(
        json.dumps({"cell": "N1", "study_id": "n1-openhands-linear"})
    )
    b_id = uuid4()
    (tmp_path / str(b_id)).mkdir()
    (tmp_path / str(b_id) / "run_manifest.json").write_text(
        json.dumps({"cell": "B4", "study_id": "b4-boss-manager-worker"})
    )
    # Then
    assert loading.detect_topology(n_id, runs_dir=tmp_path) == "linear"
    assert loading.detect_topology(b_id, runs_dir=tmp_path) == "bef"


def test_detect_topology_raises_without_signal(tmp_path) -> None:
    """An undecidable manifest raises rather than guessing."""
    # Given
    run_id = uuid4()
    (tmp_path / str(run_id)).mkdir()
    (tmp_path / str(run_id) / "run_manifest.json").write_text(json.dumps({"cell": "A2"}))
    # Then
    with pytest.raises(ValueError, match="Cannot determine topology"):
        loading.detect_topology(run_id, runs_dir=tmp_path)
