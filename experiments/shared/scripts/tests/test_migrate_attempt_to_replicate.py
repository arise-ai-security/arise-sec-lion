"""Tests for `experiments.shared.scripts.migrate_attempt_to_replicate`.

The migration is supposed to be idempotent and forgiving: a manifest that
already carries both fields stays untouched, malformed JSON is logged and
skipped without crashing, and a second run produces a byte-identical file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from experiments.shared.scripts.migrate_attempt_to_replicate import _migrate_one


if TYPE_CHECKING:
    from pathlib import Path


def _write_manifest(run_dir: Path, payload: dict) -> Path:
    """Seed a `run_manifest.json` and return its path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run_manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return manifest


def test_migrate_one_copies_attempt_to_replicate(repo_root: Path) -> None:
    # Given: a manifest with `attempt=3` and no `replicate`.
    manifest = _write_manifest(
        repo_root / "runs" / "run-1",
        {"run_id": "run-1", "attempt": 3, "kind": "ours"},
    )

    # When: the migrator runs once.
    changed = _migrate_one(manifest)

    # Then: the manifest now carries `replicate=3` and the change flag is True.
    assert changed is True
    payload = json.loads(manifest.read_text())
    assert payload["replicate"] == 3
    # And: the legacy attempt field is preserved (alias kept for one cycle).
    assert payload["attempt"] == 3


def test_migrate_one_is_idempotent(repo_root: Path) -> None:
    # Given: a freshly-migrated manifest.
    manifest = _write_manifest(
        repo_root / "runs" / "run-1",
        {"run_id": "run-1", "attempt": 3, "kind": "ours"},
    )
    _migrate_one(manifest)
    after_first = manifest.read_bytes()

    # When: the migrator runs a second time.
    changed_again = _migrate_one(manifest)

    # Then: it reports no change and the file bytes are identical.
    assert changed_again is False
    assert manifest.read_bytes() == after_first


def test_migrate_one_leaves_already_migrated_manifests_alone(repo_root: Path) -> None:
    # Given: a manifest that already carries both `attempt` and `replicate`.
    manifest = _write_manifest(
        repo_root / "runs" / "run-1",
        {"run_id": "run-1", "attempt": 5, "replicate": 5, "kind": "ours"},
    )
    before = manifest.read_bytes()

    # When
    changed = _migrate_one(manifest)

    # Then: untouched, both in change flag and on-disk bytes.
    assert changed is False
    assert manifest.read_bytes() == before


def test_migrate_one_skips_manifest_without_attempt(repo_root: Path) -> None:
    # Given: a manifest that has neither `attempt` nor `replicate` (e.g. a
    # historical run that was never enrolled into a study).
    manifest = _write_manifest(
        repo_root / "runs" / "run-1",
        {"run_id": "run-1", "kind": "ours"},
    )
    before = manifest.read_bytes()

    # When
    changed = _migrate_one(manifest)

    # Then: nothing to copy, file unchanged.
    assert changed is False
    assert manifest.read_bytes() == before


def test_migrate_one_skips_malformed_json_without_crashing(repo_root: Path) -> None:
    # Given: a manifest with malformed JSON content.
    run_dir = repo_root / "runs" / "run-broken"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "run_manifest.json"
    manifest.write_text("{not-valid-json")
    before = manifest.read_bytes()

    # When
    changed = _migrate_one(manifest)

    # Then: returns False (skip) instead of raising, and the file is untouched.
    assert changed is False
    assert manifest.read_bytes() == before


def test_migrate_one_skips_non_object_json(repo_root: Path) -> None:
    # Given: a manifest that's a JSON list, not an object.
    run_dir = repo_root / "runs" / "run-list"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "run_manifest.json"
    manifest.write_text("[1, 2, 3]")
    before = manifest.read_bytes()

    # When
    changed = _migrate_one(manifest)

    # Then: returns False, file untouched.
    assert changed is False
    assert manifest.read_bytes() == before
