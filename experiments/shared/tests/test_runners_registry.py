"""Tests for the runner registry and its `aris` runner stub.

The registry is module-level state. Each test mutates a *copy* via
monkeypatch (or restores after). For dispatch tests we monkey-patch
`harness.run_ours` and `harness.run_baseline` to return sentinel UUIDs so
no real subprocess is spawned.
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


def test_aris_dispatches_a_cell_to_run_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a sentinel UUID returned by a stubbed `run_baseline`.
    sentinel = uuid4()
    captured: dict[str, object] = {}

    def _fake_run_baseline(**kwargs):
        captured.update(kwargs)
        return sentinel

    def _fake_run_ours(**_kwargs):
        pytest.fail("run_ours must not be called for A-cells")

    monkeypatch.setattr(harness, "run_baseline", _fake_run_baseline)
    monkeypatch.setattr(harness, "run_ours", _fake_run_ours)

    aris = runners.get("aris")

    # When: we dispatch an A1 cell.
    result = aris.run(
        study_id="study-x",
        cell="A1",
        task="cve-a",
        replicate=0,
        config=Path("ignored.yaml"),
        context_file=Path("ignored.json"),
    )

    # Then: the sentinel comes back and the harness call carried the expected
    # legacy variant + replicate-as-attempt mapping.
    assert result == sentinel
    assert captured["variant"] == "claude-code-subagent"
    assert captured["cell"] == "A1"
    assert captured["attempt"] == 0
    assert captured["task"] == "cve-a"


def test_aris_dispatches_b_cell_to_run_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a sentinel returned by `run_ours` and an asserting `run_baseline`.
    sentinel = uuid4()
    captured: dict[str, object] = {}

    def _fake_run_ours(**kwargs):
        captured.update(kwargs)
        return sentinel

    def _fake_run_baseline(**_kwargs):
        pytest.fail("run_baseline must not be called for B-cells")

    monkeypatch.setattr(harness, "run_ours", _fake_run_ours)
    monkeypatch.setattr(harness, "run_baseline", _fake_run_baseline)

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

    # Then: run_ours was called with the resolved config and replicate-as-attempt.
    assert result == sentinel
    assert captured["config"] == config_path
    assert captured["cell"] == "B1"
    assert captured["attempt"] == 2


def test_aris_rejects_unknown_a_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a stubbed run_baseline that should never be called.
    monkeypatch.setattr(
        harness,
        "run_baseline",
        lambda **_: pytest.fail("run_baseline must not be called for unknown A-cells"),
    )

    aris = runners.get("aris")

    # When/Then: an A-cell without a legacy variant entry raises.
    with pytest.raises(ValueError, match="legacy baseline variant"):
        aris.run(
            study_id="study-x",
            cell="A99",
            task="cve-a",
            replicate=0,
            config=Path("ignored.yaml"),
            context_file=Path("ignored.json"),
        )


def test_aris_runner_implements_protocol() -> None:
    # Given/When/Then: the registered aris instance satisfies the runtime-checkable
    # Runner protocol (presence of id/label attrs and a `run` method).
    aris = runners.get("aris")
    assert isinstance(aris, runners.Runner)


def test_aris_legacy_variants_table_matches_known_a_cells() -> None:
    """A1/A2 must be present until PR 4b removes the legacy variant table."""
    assert set(aris_module._LEGACY_VARIANTS) == {"A1", "A2"}


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
