"""Tests for ``infrastructure.snapshot.snapshot_effective_config``."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from config.settings import Settings
from infrastructure.snapshot import SNAPSHOT_FILENAME, snapshot_effective_config


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _make_settings() -> Settings:
    return Settings.from_yaml(BASE_CONFIG)


def test_snapshot_writes_effective_config(tmp_path: Path) -> None:
    # Given: a real Settings object loaded from the repo's base config.
    run_id = uuid4()
    settings = _make_settings()

    # When: we snapshot it into a fresh run directory.
    target = snapshot_effective_config(run_id=run_id, settings=settings, run_dir=tmp_path)

    # Then: the file lives at run_dir/effective_config.yaml.
    assert target == tmp_path / SNAPSHOT_FILENAME
    assert target.is_file()

    # And: the content is valid YAML carrying the expected top-level sections.
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert payload["run_id"] == str(run_id)
    assert "boss" in payload
    assert "manager" in payload
    assert "worker" in payload
    assert "database" in payload

    # And: the sensitive postgres password is redacted.
    assert payload["database"]["password"] == "***"  # noqa: S105 - redaction sentinel
    assert payload["database"]["password"] != "test_pw"  # noqa: S105 - test fixture


def test_snapshot_creates_run_dir_if_missing(tmp_path: Path) -> None:
    # Given: a run directory that does not yet exist.
    run_dir = tmp_path / "runs" / str(uuid4())
    assert not run_dir.exists()

    # When: snapshot is called.
    snapshot_effective_config(run_id=uuid4(), settings=_make_settings(), run_dir=run_dir)

    # Then: the directory was created and the file is there.
    assert run_dir.is_dir()
    assert (run_dir / SNAPSHOT_FILENAME).is_file()


def test_snapshot_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    # Given: an existing snapshot file with stale content.
    target = tmp_path / SNAPSHOT_FILENAME
    target.write_text("stale: true\n", encoding="utf-8")

    # When: snapshot writes a fresh payload.
    snapshot_effective_config(run_id=uuid4(), settings=_make_settings(), run_dir=tmp_path)

    # Then: the content has been replaced with the new YAML.
    fresh = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "stale" not in fresh
    assert "boss" in fresh

    # And: no .tmp leftover sits beside it (atomic-write contract).
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != SNAPSHOT_FILENAME]
    assert leftovers == []
