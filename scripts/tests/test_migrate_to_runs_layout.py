"""Tests for `scripts/migrate_to_runs_layout.py`.

Uses a tmp_path-rooted fake repo and monkeypatches the module-level path
constants so the real workspace is never touched. All migrations are
idempotent by construction: every test reruns the migration and asserts
stable state.
"""

from __future__ import annotations

import json
import tarfile
import uuid
from typing import TYPE_CHECKING

import pytest
import yaml

from experiments.shared.scripts import _paths
from scripts import migrate_to_runs_layout as migration


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute every module-level path constant to subdirs under tmp_path."""
    monkeypatch.setattr(migration, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(migration, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(migration, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(migration, "LEGACY_POOL", tmp_path / "runs" / "_legacy")
    monkeypatch.setattr(migration, "DATASET_FINAL", tmp_path / "dataset-final")
    monkeypatch.setattr(migration, "DATASETS_LEGACY", tmp_path / "datasets-legacy")
    monkeypatch.setattr(migration, "LOGS_RUN_EVAL", tmp_path / "logs" / "run_evaluation")
    monkeypatch.setattr(
        migration,
        "BRIEFING_OLD",
        tmp_path / "experiments" / "configs" / "domain_briefing.md",
    )
    monkeypatch.setattr(
        migration,
        "BRIEFING_NEW",
        tmp_path / "prompts" / "domains" / "secbench" / "briefing.md",
    )
    monkeypatch.setattr(
        migration,
        "DEPLOYMENT_HTML_OLD",
        tmp_path / "deployment" / "output" / "data-flow-viz.html",
    )
    monkeypatch.setattr(
        migration,
        "DEPLOYMENT_HTML_NEW",
        tmp_path / "agent-docs" / "data-flow-viz.html",
    )
    monkeypatch.setattr(
        migration,
        "STALE_EXPERIMENTS_SUBTREES",
        (
            tmp_path / "experiments" / "__pycache__",
            tmp_path / "experiments" / "baselines",
            tmp_path / "experiments" / "tests",
            tmp_path / "experiments" / "meeting-notes",
        ),
    )
    return tmp_path


def _write_seed_study(
    fake_repo: Path,
    *,
    study_id: str,
    cells: tuple[str, ...] = ("A1", "A2", "B1", "B2"),
    default_cves: tuple[str, ...] = ("gpac.cve-2021-40575",),
) -> Path:
    """Create a minimal seed study directory that ``register_run`` accepts.

    register_run validates (a) `cell` is declared in study.cells, and
    (b) `task` is in the effective CVE set (default_cves unioned with
    per_cell_overrides). Both files must be present and well-formed or
    enrollment silently fails — looking like the migration didn't enrol
    when in fact the fixture was the problem.
    """
    study_dir = fake_repo / "experiments" / study_id
    study_dir.mkdir(parents=True)

    manifest = {
        "study_id": study_id,
        "dataset": "dataset.yaml",
        "cells": {cell: {"harness": "ours"} for cell in cells},
        "runs": [],
    }
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    dataset = {
        "name": "fake-dataset",
        "default_cves": list(default_cves),
        "per_cell_overrides": {},
    }
    (study_dir / "dataset.yaml").write_text(
        yaml.safe_dump(dataset, sort_keys=False), encoding="utf-8"
    )
    return study_dir


def _stub_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]
) -> None:
    """Record every non-git ``subprocess.run`` without executing it."""
    import subprocess as _sp

    real_run = _sp.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        # `git rev-parse HEAD` is called at migration startup; let it through
        # so we observe real subprocess behaviour rather than a stubbed OK.
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "rev-parse":
            return real_run(cmd, *args, **kwargs)
        calls.append([str(c) for c in cmd])
        return _sp.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(migration.subprocess, "run", fake_run)


def test_rename_output_to_runs(fake_repo: Path) -> None:
    # Given: an existing output dir with two run subdirs.
    (fake_repo / "output" / "uuid-1").mkdir(parents=True)
    (fake_repo / "output" / "uuid-2").mkdir(parents=True)

    # When
    migration.run(dry_run=False)

    # Then: runs/ now contains both; output/ is gone.
    assert (fake_repo / "runs" / "uuid-1").is_dir()
    assert (fake_repo / "runs" / "uuid-2").is_dir()
    assert not (fake_repo / "output").exists()


def test_rename_is_idempotent(fake_repo: Path) -> None:
    # Given: the layout is already migrated.
    (fake_repo / "runs" / "uuid-1").mkdir(parents=True)

    # When: we run twice.
    migration.run(dry_run=False)
    migration.run(dry_run=False)

    # Then: runs/ still has uuid-1.
    assert (fake_repo / "runs" / "uuid-1").is_dir()


def test_migrate_b_cell_preserves_agent_uuid(fake_repo: Path) -> None:
    # Given: a dataset-final B-cell with an agent-uuid subdir.
    agent_uuid = "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    src = (
        fake_repo / "dataset-final" / "runs" / "gpac.cve-2021-40575" / "B1" / "0"
        / agent_uuid
    )
    src.mkdir(parents=True)
    (src / "stdout_stderr.log").write_text("boss log")
    (src / "src").mkdir()

    # When
    migration.run(dry_run=False)

    # Then: the run landed under runs/_legacy/<agent-uuid>/.
    legacy = fake_repo / "runs" / "_legacy" / agent_uuid
    assert (legacy / "src").is_dir()
    assert (legacy / "stdout_stderr.log").read_text() == "boss log"

    # And: a synthesized manifest carries the expected fields.
    manifest = json.loads((legacy / "run_manifest.json").read_text())
    assert manifest["run_id"] == agent_uuid
    assert manifest["kind"] == "ours"
    assert manifest["cell"] == "B1"
    assert manifest["task"] == "gpac.cve-2021-40575"
    assert manifest["attempt"] == 0
    assert manifest["legacy_migration"] is True

    # And: spec §9 says "move", not "copy" — the source is gone.
    assert not src.exists()


def test_migrate_a_cell_mints_stable_uuid(fake_repo: Path) -> None:
    # Given: a dataset-final A-cell attempt (no agent-uuid layer).
    attempt_dir = (
        fake_repo / "dataset-final" / "runs" / "gpac.cve-2021-40575" / "A1" / "0"
    )
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "stdout_stderr.log").write_text("baseline log")
    (attempt_dir / "artifacts").mkdir()

    # When
    report = migration.run(dry_run=False)

    # Then: a legacy/<uuid>/ dir exists with manifest kind=claude_code_baseline.
    mint = migration._stable_uuid_from_path("a", "gpac.cve-2021-40575", "A1", "0")
    legacy = fake_repo / "runs" / "_legacy" / str(mint)
    assert legacy.is_dir()
    manifest = json.loads((legacy / "run_manifest.json").read_text())
    assert manifest["kind"] == "claude_code_baseline"
    assert manifest["variant"] == "claude-code-subagent"
    assert manifest["cell"] == "A1"
    # And: the minted UUID is stable across re-runs (idempotency).
    migration.run(dry_run=False)
    assert legacy.is_dir()
    assert str(mint) in str(report.legacy_run_ids)


def test_collision_aborts(fake_repo: Path) -> None:
    # Given: an already-populated legacy pool entry that lacks the migration
    # marker, AND a dataset-final run whose target UUID matches — a true
    # non-idempotent collision.
    agent_uuid = "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    (fake_repo / "runs" / "_legacy" / agent_uuid).mkdir(parents=True)
    src = (
        fake_repo / "dataset-final" / "runs" / "task.x" / "B1" / "0" / agent_uuid
    )
    src.mkdir(parents=True)

    # When/Then
    with pytest.raises(FileExistsError):
        migration.run(dry_run=False)


def test_step1_collision_aborts_before_any_move(fake_repo: Path) -> None:
    # Given: output/ has two entries; runs/ already contains a matching name —
    # the late-conflict case Phase A must catch before any move happens.
    (fake_repo / "output" / "uuid-a").mkdir(parents=True)
    (fake_repo / "output" / "uuid-b").mkdir(parents=True)
    (fake_repo / "runs" / "uuid-b").mkdir(parents=True)

    # When/Then
    with pytest.raises(FileExistsError):
        migration.run(dry_run=False)
    # And: output/uuid-a was not partially moved (no partial mutation).
    assert (fake_repo / "output" / "uuid-a").is_dir()
    assert (fake_repo / "output" / "uuid-b").is_dir()
    assert not (fake_repo / "runs" / "uuid-a").exists()


def test_tar_datasets_legacy(fake_repo: Path) -> None:
    # Given: a datasets-legacy entry.
    legacy_src = fake_repo / "datasets-legacy" / "dataset-v2"
    legacy_src.mkdir(parents=True)
    (legacy_src / "file.txt").write_text("old data")

    # When
    migration.run(dry_run=False)

    # Then: the tarball exists and contains the dir.
    archive = fake_repo / "runs" / "_legacy" / "dataset-v2.tar.gz"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("dataset-v2/file.txt") for n in names)


def test_tar_archive_already_present_is_skipped(fake_repo: Path) -> None:
    # Given: a datasets-legacy entry AND a pre-existing archive at the target.
    legacy_src = fake_repo / "datasets-legacy" / "dataset-v2"
    legacy_src.mkdir(parents=True)
    (legacy_src / "file.txt").write_text("fresh data")

    (fake_repo / "runs" / "_legacy").mkdir(parents=True)
    sentinel = b"preexisting tar bytes"
    (fake_repo / "runs" / "_legacy" / "dataset-v2.tar.gz").write_bytes(sentinel)

    # When
    migration.run(dry_run=False)

    # Then: the pre-existing archive was not overwritten.
    assert (
        fake_repo / "runs" / "_legacy" / "dataset-v2.tar.gz"
    ).read_bytes() == sentinel


def test_legacy_readme_written(fake_repo: Path) -> None:
    # Given: any migration target that causes _legacy/ to be created.
    (fake_repo / "datasets-legacy" / "dataset-v2").mkdir(parents=True)
    (fake_repo / "datasets-legacy" / "dataset-v2" / "file.txt").write_text("x")

    # When
    migration.run(dry_run=False)

    # Then
    readme = fake_repo / "runs" / "_legacy" / "README.md"
    assert readme.is_file()
    text = readme.read_text()
    assert "Legacy runs" in text
    assert "tar.gz" in text


def test_briefing_relocation(fake_repo: Path) -> None:
    # Given: the legacy briefing file.
    old = fake_repo / "experiments" / "configs" / "domain_briefing.md"
    old.parent.mkdir(parents=True)
    old.write_text("# briefing")

    # When
    migration.run(dry_run=False)

    # Then
    new = fake_repo / "prompts" / "domains" / "secbench" / "briefing.md"
    assert new.read_text() == "# briefing"
    assert not old.exists()


def test_briefing_collision_identical_bytes_unlinks_old(fake_repo: Path) -> None:
    # Given: both briefing files exist with identical content —
    # an idempotent re-run scenario.
    payload = "# briefing (identical)\n"
    old = fake_repo / "experiments" / "configs" / "domain_briefing.md"
    old.parent.mkdir(parents=True)
    old.write_text(payload)
    new = fake_repo / "prompts" / "domains" / "secbench" / "briefing.md"
    new.parent.mkdir(parents=True)
    new.write_text(payload)

    # When
    migration.run(dry_run=False)

    # Then: the old file is gone, the new file preserved verbatim.
    assert not old.exists()
    assert new.read_text() == payload


def test_briefing_collision_different_bytes_raises(fake_repo: Path) -> None:
    # Given: both briefing files exist with DIFFERENT content. Silently
    # unlinking the old would discard real data.
    old = fake_repo / "experiments" / "configs" / "domain_briefing.md"
    old.parent.mkdir(parents=True)
    old.write_text("# legacy content\n")
    new = fake_repo / "prompts" / "domains" / "secbench" / "briefing.md"
    new.parent.mkdir(parents=True)
    new.write_text("# migrated content\n")

    # When/Then
    with pytest.raises(FileExistsError):
        migration.run(dry_run=False)
    # And: neither file was modified.
    assert old.read_text() == "# legacy content\n"
    assert new.read_text() == "# migrated content\n"


def test_cleanup_removes_empty_stale_trees(fake_repo: Path) -> None:
    # Given: empty logs/run_evaluation and experiments/meeting-notes.
    (fake_repo / "logs" / "run_evaluation").mkdir(parents=True)
    (fake_repo / "experiments" / "meeting-notes").mkdir(parents=True)
    (fake_repo / "experiments" / "__pycache__").mkdir(parents=True)

    # When
    migration.run(dry_run=False)

    # Then
    assert not (fake_repo / "logs" / "run_evaluation").exists()
    assert not (fake_repo / "experiments" / "meeting-notes").exists()
    assert not (fake_repo / "experiments" / "__pycache__").exists()


def test_cleanup_removes_stale_trees_with_nested_junk(fake_repo: Path) -> None:
    # Given: logs/run_evaluation mirrors the real repo's layout — empty files
    # inside nested dirs that blocked the previous "empty or only __pycache__"
    # rule. And: experiments/baselines has only __pycache__ + empty tests dir.
    logs_root = fake_repo / "logs" / "run_evaluation"
    (logs_root / "arise-run1").mkdir(parents=True)
    (logs_root / "arise-run1" / "run_instance.log").write_text("")
    (logs_root / "arise-swebench-run1").mkdir()
    (logs_root / "arise-swebench-run1" / "log.txt").write_text("")

    baselines = fake_repo / "experiments" / "baselines"
    (baselines / "__pycache__").mkdir(parents=True)
    (baselines / "__pycache__" / "m.cpython-312.pyc").write_text("")
    (baselines / "tests").mkdir()

    # When
    migration.run(dry_run=False)

    # Then
    assert not (fake_repo / "logs" / "run_evaluation").exists()
    assert not (fake_repo / "experiments" / "baselines").exists()


def test_cleanup_preserves_stale_tree_with_real_py_source(
    fake_repo: Path,
) -> None:
    # Given: a stale subtree that contains real Python source — the probe
    # must refuse to delete it.
    target = fake_repo / "experiments" / "baselines"
    target.mkdir(parents=True)
    (target / "runner.py").write_text("print('real code')\n")

    # When
    migration.run(dry_run=False)

    # Then: the directory is preserved.
    assert (target / "runner.py").is_file()


def test_dry_run_touches_nothing(fake_repo: Path) -> None:
    # Given
    (fake_repo / "output" / "uuid-1").mkdir(parents=True)
    agent = "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    src = fake_repo / "dataset-final" / "runs" / "t.x" / "B1" / "0" / agent
    src.mkdir(parents=True)

    # When
    migration.run(dry_run=True)

    # Then: originals still present, targets absent.
    assert (fake_repo / "output" / "uuid-1").is_dir()
    assert src.is_dir()
    assert not (fake_repo / "runs").exists()


def test_dry_run_plan_covers_every_step(fake_repo: Path) -> None:
    # Given: every step has a real input to plan against, including the seed
    # study (so step 8 enrollment and step 9 report refresh are emitted).
    study_id = migration._today_study_id()
    (fake_repo / "output" / "uuid-1").mkdir(parents=True)
    attempt = (
        fake_repo / "dataset-final" / "runs" / "gpac.cve-2021-40575" / "B1" / "0"
        / "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    )
    attempt.mkdir(parents=True)
    (fake_repo / "datasets-legacy" / "dataset-v2").mkdir(parents=True)
    (fake_repo / "datasets-legacy" / "dataset-v2" / "f.txt").write_text("x")
    briefing_old = fake_repo / "experiments" / "configs" / "domain_briefing.md"
    briefing_old.parent.mkdir(parents=True)
    briefing_old.write_text("# b")
    (fake_repo / "deployment" / "output" / "data-flow-viz.html").parent.mkdir(
        parents=True
    )
    (fake_repo / "deployment" / "output" / "data-flow-viz.html").write_text(
        "<html></html>"
    )
    (fake_repo / "logs" / "run_evaluation").mkdir(parents=True)
    _write_seed_study(fake_repo, study_id=study_id)

    # When
    report = migration.run(dry_run=True)

    # Then: the in-memory plan names every step, and disk is untouched.
    combined = "\n".join(report.actions)
    assert "renaming" in combined or "moving" in combined  # step 1
    assert "migrating" in combined  # step 2
    assert "archiving" in combined  # step 3
    assert "writing" in combined  # step 4: legacy pool readme
    assert "README.md" in combined  # step 4: legacy pool readme
    assert "briefing" in combined  # step 5
    assert "deleting" in combined  # step 6
    assert "enrolling legacy run" in combined  # step 8
    assert "refreshing seed study reports" in combined  # step 9

    # And: nothing has actually moved.
    assert (fake_repo / "output" / "uuid-1").is_dir()
    assert attempt.is_dir()
    assert not (fake_repo / "runs" / "_legacy").exists()


def test_stable_uuid_derivation_is_deterministic() -> None:
    # Given: the same path components.
    a = migration._stable_uuid_from_path("a", "task", "A1", "0")
    b = migration._stable_uuid_from_path("a", "task", "A1", "0")
    c = migration._stable_uuid_from_path("a", "task", "A1", "1")
    # Then
    assert a == b
    assert a != c
    assert isinstance(a, uuid.UUID)


def test_stable_uuid_pinned_for_known_path() -> None:
    # Given: the canonical A-cell path for the seed study's reference run.
    # When/Then: the minted UUID is pinned. A regression here means the seed
    # study's manifest.yaml is out of sync with this derivation.
    pinned = "b6e3862a-deef-41df-945e-db75b73ddbb2"
    assert str(
        migration._stable_uuid_from_path("a", "gpac.cve-2021-40575", "A1", "0")
    ) == pinned


def test_enrollment_populates_study_runs(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a seed study matching the study_id the migration will mint,
    # plus a B-cell dataset-final run the migration will enroll.
    study_id = migration._today_study_id()
    _write_seed_study(fake_repo, study_id=study_id)
    # register_run reads get_repo_root() to find experiments/<study>/. Point
    # it at the fake repo root.
    monkeypatch.setattr(_paths, "REPO_ROOT", fake_repo)

    agent_uuid = "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    src = (
        fake_repo / "dataset-final" / "runs" / "gpac.cve-2021-40575" / "B1" / "0"
        / agent_uuid
    )
    src.mkdir(parents=True)
    (src / "src").mkdir()

    # Guard the step-9 subprocess call — not testing the scripts here.
    calls: list[list[str]] = []
    _stub_subprocess_run(monkeypatch, calls)

    # When
    migration.run(dry_run=False)

    # Then: the seed study manifest's runs list contains the enrolled run.
    manifest = yaml.safe_load(
        (fake_repo / "experiments" / study_id / "manifest.yaml").read_text()
    )
    run_ids = {row["run_id"] for row in manifest.get("runs") or []}
    assert agent_uuid in run_ids


def test_enrollment_skipped_when_study_absent(fake_repo: Path) -> None:
    # Given: no seed study dir, but a migratable legacy run.
    agent_uuid = "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    src = (
        fake_repo / "dataset-final" / "runs" / "gpac.cve-2021-40575" / "B1" / "0"
        / agent_uuid
    )
    src.mkdir(parents=True)

    # When
    report = migration.run(dry_run=False)

    # Then: the run still migrated, but no EnrollLegacyRun action appeared
    # (the plan-builder gates on the study manifest existing).
    legacy = fake_repo / "runs" / "_legacy" / agent_uuid
    assert legacy.is_dir()
    assert not any("enrolling" in msg for msg in report.actions)


def test_refresh_reports_invokes_every_script(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a seed study with all three report scripts.
    study_id = migration._today_study_id()
    study_dir = _write_seed_study(fake_repo, study_id=study_id)
    scripts_dir = study_dir / "scripts"
    scripts_dir.mkdir()
    for name in ("collect.py", "plot_success.py", "render_report.py"):
        (scripts_dir / name).write_text("# stub\n")
    monkeypatch.setattr(_paths, "REPO_ROOT", fake_repo)

    agent_uuid = "4fb43a9c-6ca6-4013-8b7e-79af250bad62"
    (
        fake_repo / "dataset-final" / "runs" / "gpac.cve-2021-40575" / "B1" / "0"
        / agent_uuid
    ).mkdir(parents=True)

    calls: list[list[str]] = []
    _stub_subprocess_run(monkeypatch, calls)

    # When
    migration.run(dry_run=False)

    # Then: every report script was invoked as a Python subprocess.
    script_names = {
        cmd[-1].rsplit("/", 1)[-1]
        for cmd in calls
        if any(
            name in cmd[-1]
            for name in ("collect.py", "plot_success.py", "render_report.py")
        )
    }
    assert script_names == {"collect.py", "plot_success.py", "render_report.py"}
