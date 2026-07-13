"""Containerized regression plan and runner tests."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from experiments.shared.evaluation.regression import (
    ContainerRegressionRunner,
    FrozenRegressionPlan,
    RegressionCommand,
    load_regression_plans_file,
    plan_sha256,
    unavailable_regression,
)


def _plan(
    *,
    instance_id: str = "faad2.cve-2018-20196",
    base_commit: str = "deadbeef",
    argv: tuple[str, ...] = ("project-smoke",),
    timeout: float = 5.0,
    required: bool = True,
    cwd: str = ".",
) -> FrozenRegressionPlan:
    commands = (
        RegressionCommand(
            argv=argv, timeout_seconds=timeout, required=required, cwd=cwd
        ),
    )
    work_dir = "/src/faad2"
    return FrozenRegressionPlan(
        instance_id=instance_id,
        base_commit=base_commit,
        commands=commands,
        plan_sha256=plan_sha256(
            instance_id, base_commit, commands, work_dir=work_dir
        ),
        generated_at="2026-07-12T00:00:00Z",
        work_dir=work_dir,
        generator_model="test-generator",
    )


def _docker_sequence(monkeypatch: pytest.MonkeyPatch, results: list[object]) -> list[list[str]]:
    """Stub docker CLI calls; each item is CompletedProcess or TimeoutExpired."""
    calls: list[list[str]] = []
    queue = list(results)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        del kwargs
        assert argv[0] == "docker"
        calls.append(list(argv[1:]))
        if not queue:
            raise AssertionError(f"unexpected docker call: {argv}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(
        "experiments.shared.evaluation.regression.subprocess.run", fake_run
    )
    return calls


def _ok(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=(), returncode=returncode, stdout=stdout, stderr="")


def test_plan_sha256_is_stable_and_rejects_tamper() -> None:
    plan = _plan()
    same = plan_sha256(
        plan.instance_id,
        plan.base_commit,
        plan.commands,
        work_dir=plan.work_dir,
    )
    assert plan.plan_sha256 == same
    with pytest.raises(ValueError, match="plan_sha256 mismatch"):
        FrozenRegressionPlan(
            instance_id=plan.instance_id,
            base_commit=plan.base_commit,
            commands=plan.commands,
            plan_sha256="0" * 64,
            generated_at=plan.generated_at,
            work_dir=plan.work_dir,
        )


def test_container_runner_passes_on_project_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _docker_sequence(
        monkeypatch,
        [
            _ok("sha256:img\n"),  # image inspect
            _ok("ctr-abc123\n"),  # create
            _ok(),  # start
            _ok("deadbeef\n"),  # git rev-parse
            _ok(),  # exec project-smoke
            _ok(),  # rm
        ],
    )
    evidence = ContainerRegressionRunner(apply_patch=False, rebuild=False).run(
        _plan(),
        patch_file=None,
        patch_hash="p" * 64,
        image="hwiwonlee/secb.eval.x86_64.faad2.cve-2018-20196:patch",
    )
    assert evidence.available
    assert evidence.passed
    assert evidence.container_id == "ctr-abc123"
    assert evidence.image_digest == "sha256:img"
    assert evidence.results[0].exit_code == 0
    assert any(call[:1] == ["create"] for call in calls)
    assert any(call[:1] == ["exec"] for call in calls)
    assert any(call[:2] == ["rm", "-f"] for call in calls)


def test_container_runner_fails_on_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_sequence(
        monkeypatch,
        [
            _ok("sha256:img\n"),
            _ok("ctr-1\n"),
            _ok(),
            _ok("deadbeef\n"),
            _ok(returncode=1),  # exec false
            _ok(),  # rm
        ],
    )
    evidence = ContainerRegressionRunner(apply_patch=False, rebuild=False).run(
        _plan(argv=("false",)),
        patch_file=None,
        patch_hash=None,
    )
    assert evidence.available
    assert not evidence.passed
    assert "required regression command failed" in evidence.reason


def test_container_runner_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_sequence(
        monkeypatch,
        [
            _ok("sha256:img\n"),
            _ok("ctr-1\n"),
            _ok(),
            _ok("deadbeef\n"),
            subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=0.1),
            _ok(),  # rm
        ],
    )
    evidence = ContainerRegressionRunner(apply_patch=False, rebuild=False).run(
        _plan(argv=("sleep", "2"), timeout=0.1),
        patch_file=None,
        patch_hash=None,
    )
    assert evidence.available
    assert not evidence.passed
    assert evidence.results[0].timed_out


def test_container_runner_rejects_cwd_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_sequence(
        monkeypatch,
        [
            _ok("sha256:img\n"),
            _ok("ctr-1\n"),
            _ok(),
            _ok("deadbeef\n"),
            _ok(),  # rm after escape reject
        ],
    )
    evidence = ContainerRegressionRunner(apply_patch=False, rebuild=False).run(
        _plan(cwd="../outside"),
        patch_file=None,
        patch_hash=None,
    )
    assert not evidence.available
    assert "escapes container work_dir" in evidence.reason


def test_container_runner_applies_patch_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = tmp_path / "model_patch.diff"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    calls = _docker_sequence(
        monkeypatch,
        [
            _ok("sha256:img\n"),
            _ok("ctr-1\n"),
            _ok(),  # start
            _ok("deadbeef\n"),  # git rev-parse
            _ok(),  # cp
            _ok(),  # secb patch
            _ok(),  # secb build
            _ok(),  # exec true
            _ok(),  # rm
        ],
    )
    evidence = ContainerRegressionRunner().run(
        _plan(),
        patch_file=patch,
        patch_hash=hashlib.sha256(patch.read_bytes()).hexdigest(),
    )
    assert evidence.passed
    assert any(call[:1] == ["cp"] for call in calls)
    exec_shells = [
        call for call in calls if call[:1] == ["exec"] and "secb" in " ".join(call)
    ]
    assert any("secb patch" in " ".join(call) for call in exec_shells)
    assert any("secb build" in " ".join(call) for call in exec_shells)


def test_patched_runner_requires_patch_file_and_hash() -> None:
    evidence = ContainerRegressionRunner().run(
        _plan(),
        patch_file=None,
        patch_hash=None,
    )
    assert not evidence.available
    assert "requires a patch file and SHA-256" in evidence.reason


def test_patched_runner_rejects_patch_hash_mismatch(tmp_path: Path) -> None:
    patch = tmp_path / "model_patch.diff"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    evidence = ContainerRegressionRunner().run(
        _plan(),
        patch_file=patch,
        patch_hash="0" * 64,
    )
    assert not evidence.available
    assert "patch SHA-256 mismatch" in evidence.reason


def test_load_plans_file_round_trip(tmp_path: Path) -> None:
    plan = _plan()
    base_validation = "a" * 64
    gold_validation = "b" * 64
    path = tmp_path / "regression_plans.yaml"
    plan_document = {
        "instance_id": plan.instance_id,
        "base_commit": plan.base_commit,
        "generated_at": plan.generated_at,
        "work_dir": plan.work_dir,
        "generator_model": plan.generator_model,
        "dataset_revision": "rev1",
        "image_digest": "sha256:abc",
        "base_validation_sha256": base_validation,
        "gold_validation_sha256": gold_validation,
        "plan_sha256": plan_sha256(
            plan.instance_id,
            plan.base_commit,
            plan.commands,
            work_dir=plan.work_dir,
            dataset_revision="rev1",
            image_digest="sha256:abc",
            base_validation_sha256=base_validation,
            gold_validation_sha256=gold_validation,
        ),
        "commands": [
            {
                "argv": list(plan.commands[0].argv),
                "timeout_seconds": plan.commands[0].timeout_seconds,
                "required": True,
                "cwd": ".",
            }
        ],
    }
    frozen_plan_sha = str(plan_document["plan_sha256"])
    path.write_text(
        yaml.safe_dump(
            {
                "plans": [plan_document],
                "meta": {
                    "plan_set_sha256": hashlib.sha256(
                        frozen_plan_sha.encode("utf-8")
                    ).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_regression_plans_file(path)
    loaded_plan = loaded[(plan.instance_id, plan.base_commit)]
    assert loaded_plan.plan_sha256 != plan.plan_sha256
    assert loaded_plan.image_digest == "sha256:abc"


@pytest.mark.parametrize(
    "argv",
    [
        ("true",),
        ("bash", "-lc", "true"),
        ("bash", "-lc", "test -d ."),
    ],
)
def test_trivial_regression_commands_are_rejected(argv: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="trivial regression command"):
        RegressionCommand(argv=argv, timeout_seconds=1.0)


def test_load_plan_requires_validation_provenance(tmp_path: Path) -> None:
    plan = _plan()
    path = tmp_path / "regression_plans.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "instance_id": plan.instance_id,
                "base_commit": plan.base_commit,
                "generated_at": plan.generated_at,
                "work_dir": plan.work_dir,
                "generator_model": plan.generator_model,
                "plan_sha256": plan.plan_sha256,
                "commands": [
                    {
                        "argv": list(plan.commands[0].argv),
                        "timeout_seconds": plan.commands[0].timeout_seconds,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="lacks frozen authority"):
        load_regression_plans_file(path)


def test_unavailable_regression_is_fail_closed() -> None:
    evidence = unavailable_regression(reason="missing plan", base_commit="abc")
    assert not evidence.available
    assert not evidence.passed


def test_create_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker_sequence(
        monkeypatch,
        [
            _ok("sha256:img\n"),
            _ok(returncode=1),  # create fails
        ],
    )
    evidence = ContainerRegressionRunner(apply_patch=False, rebuild=False).run(
        _plan(),
        patch_file=None,
        patch_hash=None,
    )
    assert not evidence.available
    assert "failed to create regression container" in evidence.reason
