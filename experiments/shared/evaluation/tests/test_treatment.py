"""Tests for configured treatment metadata."""

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


def test_unversioned_run_has_no_inferred_treatment(tmp_path) -> None:
    run = _run(tmp_path, None)
    original = run.events[0].model_dump()

    version = treatment_version(run)

    assert version is None
    assert run.events[0].model_dump() == original


def test_recorded_treatment_is_returned(tmp_path) -> None:
    run = _run(tmp_path, "b4")

    assert treatment_version(run) == "b4"
