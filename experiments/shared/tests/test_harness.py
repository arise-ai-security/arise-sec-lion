"""Integration tests for the study harness (spec §8 + §11).

Uses a stubbed ``_invoke_main_py`` that emulates ``python main.py run``
writing to `.last_run.json` and a manifest, and a fake baseline module
registered in ``_BASELINE_MODULES``. No real subprocesses are spawned.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import yaml

from experiments.shared import harness


if TYPE_CHECKING:
    from pathlib import Path


# -----------------------------
# Shared test helpers
# -----------------------------


def _write_study(
    repo_root: Path,
    *,
    study_id: str,
    cells: dict[str, dict],
    default_cves: list[str],
    per_cell_overrides: dict[str, dict] | None = None,
) -> Path:
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True)
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "schema_version": 2,
                "cells": cells,
                "dataset": "dataset.yaml",
            },
            sort_keys=False,
        )
    )

    source_paths: list[str] = []
    for cve in default_cves:
        # Mimic project convention: deployment/<slug>.json with dashes
        filename = cve.replace(".", "-") + ".json"
        path = repo_root / "deployment" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"instance_id": cve}))
        source_paths.append(f"deployment/{filename}")

    (study_dir / "dataset.yaml").write_text(
        yaml.safe_dump(
            {
                "default_cves": default_cves,
                "per_cell_overrides": per_cell_overrides or {},
                "source": {"kind": "deployment-json", "paths": source_paths},
            },
            sort_keys=False,
        )
    )
    return study_dir


# -----------------------------
# run-ours
# -----------------------------


@pytest.fixture
def stub_main_py_success(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace `_invoke_main_py` with a stub that mints a boss_id and exits 0."""

    state: dict[str, object] = {"boss_id": None}

    def _fake_invoke(*, config, task, context_file, python_bin=None) -> int:  # noqa: ARG001
        runs_root = harness._runs_root()
        runs_root.mkdir(parents=True, exist_ok=True)
        boss_id = uuid4()
        state["boss_id"] = boss_id
        # Simulate the runtime writing `.last_run.json` and a minimal manifest.
        (runs_root / ".last_run.json").write_text(
            json.dumps(
                {
                    "boss_id": str(boss_id),
                    "task": task,
                    "started_at": "2026-04-22T00:00:00",
                    "status": "success",
                }
            )
        )
        run_dir = runs_root / str(boss_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": str(boss_id),
                    "kind": "ours",
                    "exit_status": "success",
                    "models": {"boss": "o3", "manager": "o3", "worker": "openai/o3"},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0

    async def _fake_project_events(
        *,
        run_id,  # noqa: ARG001
        output_path,
        settings=None,  # noqa: ARG001
        config_path=None,  # noqa: ARG001
    ):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("")  # empty events.jsonl is fine for the test
        return 0

    monkeypatch.setattr(harness, "_invoke_main_py", _fake_invoke)
    monkeypatch.setattr(harness, "project_events_to_jsonl", _fake_project_events)
    return state


def test_run_ours_enrolls_run_into_study(
    repo_root: Path,
    stub_main_py_success: dict[str, object],
) -> None:
    # Given: a well-formed study with one CVE and a B2 cell.
    study_id = "2026-04-22-ours-test"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"B2": {"config": "configs/B2.yaml", "harness": "ours"}},
        default_cves=["gpac.cve-2021-40575"],
    )

    # When: run-ours is invoked for that cell/task.
    run_id = harness.run_ours(
        study_id=study_id,
        cell="B2",
        task="gpac.cve-2021-40575",
        attempt=0,
        config=repo_root / "fake-config.yaml",
    )

    # Then: the stub minted the same boss_id the harness captured.
    assert run_id == stub_main_py_success["boss_id"]

    # And: the run manifest now carries experiment-level fields.
    run_manifest = json.loads(
        (repo_root / "runs" / str(run_id) / "run_manifest.json").read_text()
    )
    assert run_manifest["study_id"] == study_id
    assert run_manifest["cell"] == "B2"
    assert run_manifest["task"] == "gpac.cve-2021-40575"
    assert run_manifest["replicate"] == 0
    # And: the legacy attempt alias is preserved for one rename cycle.
    assert run_manifest["attempt"] == 0

    # And: the study manifest is design-only — register_run no longer writes
    # a `runs:` list back into it (PR 1: enrollment moved to the lockfile).
    study_manifest = yaml.safe_load(
        (repo_root / "experiments" / study_id / "manifest.yaml").read_text()
    )
    assert "runs" not in study_manifest

    # And: events.jsonl was produced (by the fake projector).
    assert (repo_root / "runs" / str(run_id) / "events.jsonl").exists()


def test_run_ours_rejects_stale_last_run_pointer(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `main.py run` exits non-zero without touching `.last_run.json`,
    the harness refuses to enroll the previous run's boss_id (codex P1)."""
    # Given: a study, and a pre-existing `.last_run.json` from an unrelated run.
    study_id = "2026-04-22-stale-test"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"B2": {"config": "configs/B2.yaml", "harness": "ours"}},
        default_cves=["gpac.cve-2021-40575"],
    )
    runs_root = repo_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    stale_boss_id = uuid4()
    (runs_root / ".last_run.json").write_text(
        json.dumps(
            {
                "boss_id": str(stale_boss_id),
                "task": "old task",
                "started_at": "2020-01-01T00:00:00",
                "status": "success",
            }
        )
    )

    def _fake_invoke_that_fails(*, config, task, context_file, python_bin=None) -> int:  # noqa: ARG001
        # Subprocess fails before RunPersistence.save_last_run() runs.
        return 1

    monkeypatch.setattr(harness, "_invoke_main_py", _fake_invoke_that_fails)

    # When/Then: harness detects the stale pointer and aborts.
    with pytest.raises(RuntimeError, match="was not advanced"):
        harness.run_ours(
            study_id=study_id,
            cell="B2",
            task="gpac.cve-2021-40575",
            attempt=0,
            config=repo_root / "fake-config.yaml",
        )


def test_run_ours_aborts_on_task_not_in_cell_scope(
    repo_root: Path,
    stub_main_py_success: dict[str, object],  # noqa: ARG001 - ensures stubs registered
) -> None:
    # Given: cell B2 subsets default_cves down to just cve-a.
    study_id = "2026-04-22-scoped"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"B2": {"config": "x", "harness": "ours"}},
        default_cves=["cve-a", "cve-b"],
        per_cell_overrides={"B2": {"subset": ["cve-a"]}},
    )

    # When/Then
    with pytest.raises(ValueError, match="not in the effective CVE set"):
        harness.run_ours(
            study_id=study_id,
            cell="B2",
            task="cve-b",
            attempt=0,
            config=repo_root / "fake-config.yaml",
        )


def test_run_ours_aborts_when_source_path_missing(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a study declaring a CVE whose source.paths entry doesn't match.
    study_id = "2026-04-22-mispath"
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True)
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "cells": {"B2": {"config": "x", "harness": "ours"}},
                "dataset": "dataset.yaml",
                "runs": [],
            },
            sort_keys=False,
        )
    )
    (study_dir / "dataset.yaml").write_text(
        yaml.safe_dump(
            {
                "default_cves": ["gpac.cve-2021-40575"],
                "source": {
                    "kind": "deployment-json",
                    "paths": ["deployment/unrelated-fixture.json"],
                },
            },
            sort_keys=False,
        )
    )

    # Ensure no subprocess is actually launched even if coverage slipped past.
    monkeypatch.setattr(
        harness,
        "_invoke_main_py",
        lambda **_: pytest.fail("main.py should never run when coverage fails"),
    )

    # When/Then
    with pytest.raises(ValueError, match="no entry matching task"):
        harness.run_ours(
            study_id=study_id,
            cell="B2",
            task="gpac.cve-2021-40575",
            attempt=0,
            config=repo_root / "fake-config.yaml",
        )


# -----------------------------
# run-baseline
# -----------------------------


@pytest.fixture
def stub_baseline_runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Register a fake baseline module in _BASELINE_MODULES for the test."""

    calls: dict[str, object] = {"ran": False, "run_dir": None, "task": None}

    def _fake_run_baseline(*, run_dir, task_prompt, context_file, **_kwargs):
        calls["ran"] = True
        calls["run_dir"] = run_dir
        calls["task"] = task_prompt
        calls["context_file"] = context_file
        (run_dir / "stdout_stderr.log").write_text("fake baseline output\n")
        return {"variant": "fake-variant", "exit_status": "success"}

    class _FakeModule:
        run_baseline = staticmethod(_fake_run_baseline)

    fake_name = "experiments.shared.baselines.fake_variant"
    monkeypatch.setattr(
        harness,
        "_BASELINE_MODULES",
        {**harness._BASELINE_MODULES, "fake-variant": fake_name},
    )
    import sys

    monkeypatch.setitem(sys.modules, fake_name, _FakeModule())
    return calls


def test_run_baseline_enrolls_newly_minted_run(
    repo_root: Path,
    stub_baseline_runner: dict[str, object],
) -> None:
    # Given: a study with an A-cell declared.
    study_id = "2026-04-22-baseline-test"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"A1": {"config": "configs/A1.yaml", "harness": "baseline"}},
        default_cves=["gpac.cve-2021-40575"],
    )

    # When: run-baseline dispatches to the fake variant.
    run_id = harness.run_baseline(
        study_id=study_id,
        cell="A1",
        task="gpac.cve-2021-40575",
        attempt=0,
        variant="fake-variant",
    )

    # Then: the baseline was invoked in a fresh runs/<uuid>/ dir.
    assert stub_baseline_runner["ran"] is True
    assert isinstance(run_id, UUID)
    run_dir = repo_root / "runs" / str(run_id)
    assert (run_dir / "stdout_stderr.log").exists()

    # And: the manifest carries baseline-flavored fields and enrollment merge.
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    assert manifest["kind"] == "claude_code_baseline"
    assert manifest["variant"] == "fake-variant"
    assert manifest["exit_status"] == "success"
    assert manifest["study_id"] == study_id
    assert manifest["cell"] == "A1"
    assert manifest["task"] == "gpac.cve-2021-40575"
    assert manifest["replicate"] == 0
    assert manifest["attempt"] == 0


def test_match_path_for_task_prefers_exact_stem() -> None:
    """Prefix-related slugs must pick the exact stem first (codex Phase 4 P2)."""
    # Given: two candidate paths where one is a prefix of the other.
    paths = [
        "deployment/cve-instances/foo-cve-2021-12345.json",  # prefix match (longer stem)
        "deployment/cve-instances/foo-cve-2021-1234.json",  # exact match
    ]
    task = "foo.cve-2021-1234"

    # When/Then: exact match is chosen, even though it appears second.
    assert harness._match_path_for_task(task, paths) == "deployment/cve-instances/foo-cve-2021-1234.json"


def test_match_path_for_task_falls_back_to_substring() -> None:
    # Given: no exact stem match.
    paths = ["deployment/cve-instances/openjpeg-cve-2016-7445-extra.json"]
    task = "openjpeg.cve-2016-7445"

    # When/Then: substring fallback picks the only candidate.
    assert (
        harness._match_path_for_task(task, paths) == "deployment/cve-instances/openjpeg-cve-2016-7445-extra.json"
    )


def test_run_baseline_rejects_unknown_variant(repo_root: Path) -> None:
    # Given: a valid study.
    study_id = "2026-04-22-unknown-variant"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"A1": {"config": "x", "harness": "baseline"}},
        default_cves=["cve-a"],
    )

    # When/Then
    with pytest.raises(ValueError, match="unknown baseline variant"):
        harness.run_baseline(
            study_id=study_id,
            cell="A1",
            task="cve-a",
            attempt=0,
            variant="not-a-real-variant",
        )


# -----------------------------
# Must-fix regression tests
# -----------------------------


def test_run_ours_builds_main_py_argv_in_exact_order(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §8: argv MUST be ``-c <config>`` BEFORE the ``run`` subcommand,
    and ``--domain-context-file`` is required for ``--domain security``."""
    # Given: a well-formed study and a captured subprocess.run call.
    study_id = "2026-04-22-argv-order"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"B2": {"config": "configs/B2.yaml", "harness": "ours"}},
        default_cves=["gpac.cve-2021-40575"],
    )

    captured: dict[str, object] = {}

    def _fake_subprocess_run(cmd, *, check, cwd):  # noqa: ARG001
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        # Simulate the runtime writing `.last_run.json` + run_manifest.
        runs_root = harness._runs_root()
        runs_root.mkdir(parents=True, exist_ok=True)
        boss_id = uuid4()
        captured["boss_id"] = boss_id
        (runs_root / ".last_run.json").write_text(
            json.dumps({"boss_id": str(boss_id), "status": "success"})
        )
        run_dir = runs_root / str(boss_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(
            json.dumps({"run_id": str(boss_id), "kind": "ours"}, sort_keys=True) + "\n"
        )

        class _Completed:
            returncode = 0

        return _Completed()

    monkeypatch.setattr(harness.subprocess, "run", _fake_subprocess_run)

    async def _fake_project_events(*, run_id, output_path, config_path=None):  # noqa: ARG001
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("")
        return 0

    monkeypatch.setattr(harness, "project_events_to_jsonl", _fake_project_events)

    config_path = repo_root / "fake-config.yaml"

    # When
    harness.run_ours(
        study_id=study_id,
        cell="B2",
        task="gpac.cve-2021-40575",
        attempt=0,
        config=config_path,
    )

    # Then: argv has the exact ordering spec §8 requires.
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert len(cmd) == 10, f"unexpected argv length {len(cmd)}: {cmd}"
    # [python, main.py, "-c", <config>, "run", <task>, "--domain", "security",
    #  "--domain-context-file", <path>]
    assert cmd[1].endswith("main.py")
    assert cmd[2] == "-c"
    assert cmd[3] == str(config_path)
    assert cmd[4] == "run"
    assert cmd[5] == "gpac.cve-2021-40575"
    assert cmd[6] == "--domain"
    assert cmd[7] == "security"
    assert cmd[8] == "--domain-context-file"
    # The context file is resolved from the dataset source.paths entry.
    assert cmd[9].endswith("gpac-cve-2021-40575.json")


def test_run_ours_aborts_when_subset_references_unknown_cve(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """per_cell_overrides subset must be ⊆ default_cves (must-fix #1)."""
    # Given: B1 subset references a CVE NOT in default_cves.
    study_id = "2026-04-22-subset-scope"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={
            "B1": {"config": "x", "harness": "ours"},
            "B2": {"config": "x", "harness": "ours"},
        },
        default_cves=["cve-a"],
        per_cell_overrides={"B1": {"subset": ["cve-ghost"]}},
    )

    # Ensure no subprocess fires if preflight slipped.
    monkeypatch.setattr(
        harness,
        "_invoke_main_py",
        lambda **_: pytest.fail("subprocess must not run when subset is invalid"),
    )

    # When/Then: harness aborts with a clear error listing offending slugs.
    with pytest.raises(ValueError, match="must be a subset of default_cves"):
        harness.run_ours(
            study_id=study_id,
            cell="B2",
            task="cve-a",
            attempt=0,
            config=repo_root / "fake-config.yaml",
        )


def test_run_ours_aborts_when_cell_not_declared(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown cell must abort before any subprocess fires (must-fix #2)."""
    # Given: a study with only B2 declared.
    study_id = "2026-04-22-unknown-cell"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"B2": {"config": "x", "harness": "ours"}},
        default_cves=["cve-a"],
    )
    monkeypatch.setattr(
        harness,
        "_invoke_main_py",
        lambda **_: pytest.fail("subprocess must not run for an unknown cell"),
    )

    # When/Then
    with pytest.raises(ValueError, match=r"not declared in study\.cells"):
        harness.run_ours(
            study_id=study_id,
            cell="Z9",
            task="cve-a",
            attempt=0,
            config=repo_root / "fake-config.yaml",
        )


def test_run_baseline_aborts_when_cell_not_declared(
    repo_root: Path,
    stub_baseline_runner: dict[str, object],
) -> None:
    """Unknown cell must abort BEFORE the baseline runs (must-fix #2)."""
    # Given: a study declaring only A1.
    study_id = "2026-04-22-baseline-unknown-cell"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"A1": {"config": "x", "harness": "baseline"}},
        default_cves=["cve-a"],
    )

    # When/Then
    with pytest.raises(ValueError, match=r"not declared in study\.cells"):
        harness.run_baseline(
            study_id=study_id,
            cell="Z9",
            task="cve-a",
            attempt=0,
            variant="fake-variant",
        )
    # And: the baseline runner was never invoked.
    assert stub_baseline_runner["ran"] is False


def test_run_baseline_retries_on_uuid_collision(
    repo_root: Path,
    stub_baseline_runner: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uuid4 collisions must retry instead of silently overwriting a run dir
    (must-fix #5)."""
    # Given: a study + a pre-existing run dir that collides with the first two
    # uuid4 values we'll mint.
    study_id = "2026-04-22-uuid-collision"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"A1": {"config": "x", "harness": "baseline"}},
        default_cves=["cve-a"],
    )

    colliding_uuid = uuid4()
    fresh_uuid = uuid4()
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)
    (runs_pool / str(colliding_uuid)).mkdir()  # taken

    calls = iter([colliding_uuid, colliding_uuid, fresh_uuid])

    def _fake_uuid4():
        return next(calls)

    monkeypatch.setattr(harness, "uuid4", _fake_uuid4)

    # When
    run_id = harness.run_baseline(
        study_id=study_id,
        cell="A1",
        task="cve-a",
        attempt=0,
        variant="fake-variant",
    )

    # Then: the fresh uuid4 is used, and the collided dir is untouched.
    assert run_id == fresh_uuid
    assert stub_baseline_runner["run_dir"] == runs_pool / str(fresh_uuid)
    # And: the colliding dir is still empty (not overwritten by the baseline).
    assert list((runs_pool / str(colliding_uuid)).iterdir()) == []


def test_run_baseline_raises_after_max_uuid_retries(
    repo_root: Path,
    stub_baseline_runner: dict[str, object],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three collisions in a row must surface as a RuntimeError (must-fix #5)."""
    study_id = "2026-04-22-uuid-retry-exhausted"
    _write_study(
        repo_root,
        study_id=study_id,
        cells={"A1": {"config": "x", "harness": "baseline"}},
        default_cves=["cve-a"],
    )

    colliding_uuid = uuid4()
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)
    (runs_pool / str(colliding_uuid)).mkdir()

    monkeypatch.setattr(harness, "uuid4", lambda: colliding_uuid)

    # When/Then
    with pytest.raises(RuntimeError, match="failed to mint a unique run_id"):
        harness.run_baseline(
            study_id=study_id,
            cell="A1",
            task="cve-a",
            attempt=0,
            variant="fake-variant",
        )


# -----------------------------
# Baseline env-sanitization (must-fix #3)
# -----------------------------


def test_claude_code_baseline_strips_secrets_from_subprocess_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline subprocess env must never include DB / non-Anthropic LLM secrets."""
    from experiments.shared.baselines import run_claude_code as baseline

    # Given: a populated process env with secrets and one allowlisted key.
    monkeypatch.setenv("POSTGRES_PASSWORD", "SECRET_DB")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("ARISE_ENV", "production")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")

    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):  # noqa: ARG001
        captured["env"] = dict(kwargs.get("env") or {})

        class _Completed:
            returncode = 0

        return _Completed()

    monkeypatch.setattr(baseline.subprocess, "run", _fake_run)
    monkeypatch.setattr(baseline.shutil, "which", lambda _cmd: "/usr/bin/claude")

    run_dir = tmp_path / "run"

    # When
    baseline.run_baseline(run_dir=run_dir, task_prompt="demo task")

    # Then: secrets are stripped.
    env = captured["env"]
    assert isinstance(env, dict)
    for stripped in (
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OLLAMA_API_KEY",
        "LLM_API_KEY",
        "ARISE_ENV",
    ):
        assert stripped not in env, f"{stripped} leaked into subprocess env"

    # And: ANTHROPIC_API_KEY is preserved (the baseline needs it).
    assert env["ANTHROPIC_API_KEY"] == "sk-anthropic"


def test_claude_code_nosubagent_strips_secrets_from_subprocess_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same allowlist applies to the nosubagent variant (must-fix #3)."""
    from experiments.shared.baselines import run_claude_code_no_subagent as baseline

    monkeypatch.setenv("POSTGRES_PASSWORD", "SECRET_DB")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")

    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):  # noqa: ARG001
        captured["env"] = dict(kwargs.get("env") or {})

        class _Completed:
            returncode = 0

        return _Completed()

    monkeypatch.setattr(baseline.subprocess, "run", _fake_run)
    monkeypatch.setattr(baseline.shutil, "which", lambda _cmd: "/usr/bin/claude")

    # When
    baseline.run_baseline(run_dir=tmp_path / "run", task_prompt="demo task")

    env = captured["env"]
    assert isinstance(env, dict)
    assert "POSTGRES_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-anthropic"
