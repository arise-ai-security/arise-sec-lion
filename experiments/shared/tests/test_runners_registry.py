"""Tests for the runner registry and its `aris` runner stub.

The registry is module-level state. Each test mutates a *copy* via
monkeypatch (or restores after). For dispatch tests we monkey-patch
`harness.run_ours` to return sentinel UUIDs so no real subprocess is
spawned.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from experiments.shared import harness, runners
from experiments.shared.runners import aris as aris_module


def test_aris_runner_is_registered() -> None:
    # Given: the package was imported, which auto-discovers runner modules.

    # When: we list known runner ids.
    ids = runners.all_ids()

    # Then: aris is registered and resolvable.
    assert "aris" in ids
    assert runners.get("aris").id == "aris"


def test_get_unknown_runner_raises_with_helpful_message() -> None:
    # Given/When/Then: KeyError mentions the missing id and the known set.
    with pytest.raises(KeyError, match="unknown runner 'bogus'") as exc_info:
        runners.get("bogus")
    assert "aris" in str(exc_info.value)


def test_register_collision_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a clean registry that already holds aris.
    monkeypatch.setattr(runners, "_REGISTRY", {"aris": runners.get("aris")})

    class _Dup:
        id = "aris"
        label = "duplicate"

        def run(self, **_kwargs):  # pragma: no cover - never invoked
            raise NotImplementedError

    # When/Then: re-registering the same id is a hard error.
    with pytest.raises(ValueError, match="runner id collision"):
        runners.register(_Dup())


def test_all_ids_returns_sorted_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a registry seeded with deliberately out-of-order ids.
    class _A:
        id = "zeta"
        label = "z"

        def run(self, **_kwargs):  # pragma: no cover
            raise NotImplementedError

    class _B:
        id = "alpha"
        label = "a"

        def run(self, **_kwargs):  # pragma: no cover
            raise NotImplementedError

    monkeypatch.setattr(runners, "_REGISTRY", {"zeta": _A(), "alpha": _B()})

    # When/Then: ids come back in lexicographic order.
    assert runners.all_ids() == ["alpha", "zeta"]


def test_aris_dispatches_a_cell_to_run_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-cells now flow through ``run_ours`` (i.e. ``main.py run``); the cell
    config selects flat-mode via ``orchestration.mode: flat``. PR 4b removed
    the legacy ``run_baseline`` short-circuit; PR 5 deleted ``run_baseline``."""
    # Given: a sentinel UUID returned by a stubbed `run_ours`.
    sentinel = uuid4()
    captured: dict[str, object] = {}

    def _fake_run_ours(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(harness, "run_ours", _fake_run_ours)

    aris = runners.get("aris")

    # When: we dispatch an A1 cell.
    config_path = Path("a1.yaml")
    result = aris.run(
        study_id="study-x",
        cell="A1",
        task="cve-a",
        replicate=0,
        config=config_path,
        context_file=Path("ignored.json"),
    )

    # Then: the sentinel comes back and run_ours saw the resolved config and
    # replicate (the cell drives flat vs. hierarchical via
    # orchestration.mode inside the config, not via the runner).
    assert result == sentinel
    assert captured["cell"] == "A1"
    assert captured["task"] == "cve-a"
    assert captured["replicate"] == 0
    assert captured["config"] == config_path


def test_aris_dispatches_b_cell_to_run_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a sentinel returned by `run_ours`.
    sentinel = uuid4()
    captured: dict[str, object] = {}

    def _fake_run_ours(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(harness, "run_ours", _fake_run_ours)

    aris = runners.get("aris")
    config_path = Path("/tmp/config.yaml")  # noqa: S108 - test sentinel only

    # When: we dispatch a B1 cell.
    result = aris.run(
        study_id="study-x",
        cell="B1",
        task="cve-b",
        replicate=2,
        config=config_path,
        context_file=Path("/tmp/ctx.json"),  # noqa: S108
    )

    # Then: run_ours was called with the resolved config and replicate.
    assert result == sentinel
    assert captured["config"] == config_path
    assert captured["cell"] == "B1"
    assert captured["replicate"] == 2


def test_aris_runner_implements_protocol() -> None:
    # Given/When/Then: the registered aris instance satisfies the runtime-checkable
    # Runner protocol (presence of id/label attrs and a `run` method).
    aris = runners.get("aris")
    assert isinstance(aris, runners.Runner)


def test_aris_module_no_longer_exposes_legacy_variants_table() -> None:
    """PR 4b removes ``_LEGACY_VARIANTS``; A-cells route through main.py now."""
    assert not hasattr(aris_module, "_LEGACY_VARIANTS")


def test_aris_returns_uuid_from_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a UUID-typed sentinel from run_ours.
    sentinel = uuid4()
    monkeypatch.setattr(harness, "run_ours", lambda **_: sentinel)

    # When: we dispatch a B-cell.
    result = runners.get("aris").run(
        study_id="study-x",
        cell="B2",
        task="cve",
        replicate=0,
        config=Path("c.yaml"),
        context_file=Path("ctx.json"),
    )

    # Then: the UUID round-trips unchanged.
    assert isinstance(result, UUID)
    assert result == sentinel
