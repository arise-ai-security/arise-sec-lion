"""Unit tests for flat-mode dispatch through the experiment runner."""

from pathlib import Path
from uuid import uuid4

import pytest

from experiments.shared import harness
from experiments.shared.runners import arise as arise_module, get as get_runner


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def test_arise_runner_delegates_to_harness_run_arise(monkeypatch: pytest.MonkeyPatch) -> None:
    """arise.run forwards every dispatch through ``harness.run_arise`` regardless of cell."""
    # Given: a stubbed run_arise that captures its arguments and returns a sentinel.
    sentinel = uuid4()
    captured: dict[str, object] = {}

    def _fake_run_arise(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(harness, "run_arise", _fake_run_arise)

    # When: dispatching flat and hierarchical cells through the same runner.
    arise = get_runner("arise")
    config_path = Path("/tmp/cell.yaml")  # noqa: S108 — test sentinel only.
    flat_result = arise.run(
        study_id="study-x",
        cell="A1",
        task="cve-flat",
        replicate=0,
        config=config_path,
        context_file=Path("ignored.json"),
    )
    hier_result = arise.run(
        study_id="study-x",
        cell="B2",
        task="cve-hier",
        replicate=2,
        config=config_path,
        context_file=Path("ignored.json"),
    )

    # Then: both cells returned the sentinel and forwarded the last call unchanged.
    assert flat_result == sentinel
    assert hier_result == sentinel
    assert captured["cell"] == "B2"
    assert captured["task"] == "cve-hier"
    assert captured["replicate"] == 2
    assert captured["config"] == config_path


def test_arise_module_no_longer_carries_legacy_variant_table() -> None:
    """The legacy ``_LEGACY_VARIANTS`` mapping is removed in PR 4b."""
    # Then: dispatch has no cell-specific legacy table.
    assert not hasattr(arise_module, "_LEGACY_VARIANTS")
