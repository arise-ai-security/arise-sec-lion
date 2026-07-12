"""Tests for immutable B4 treatment classification."""

from uuid import uuid4

from core.domain.events.events import RunStarted
from experiments.shared.evaluation.models import RunData
from experiments.shared.evaluation.treatment import treatment_version


def _run(tmp_path, version):
    run_id = uuid4()
    event = RunStarted(
        aggregate_id=run_id,
        sequence_number=1,
        task_description="task",
        treatment_version=version,
        config_hash="a" * 64 if version else None,
    )
    return RunData(run_id=run_id, events=[event], run_dir=tmp_path, manifest={})


def test_historical_unversioned_b4_is_classified_without_event_mutation(tmp_path) -> None:
    # Given: An unversioned historical B4 event
    run = _run(tmp_path, None)
    original = run.events[0].model_dump()

    # When: The treatment is classified
    version = treatment_version(run, cell="B4")

    # Then: It is reported as b4-full-v0 and the frozen event is unchanged
    assert version == "b4-full-v0"
    assert run.events[0].model_dump() == original


def test_recorded_adaptive_version_wins(tmp_path) -> None:
    # Given: A new adaptive B4 run
    run = _run(tmp_path, "b4-adaptive-v1")

    # When/Then: The persisted version is returned
    assert treatment_version(run, cell="B4") == "b4-adaptive-v1"
