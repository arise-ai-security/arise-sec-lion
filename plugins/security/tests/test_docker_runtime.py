from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.docker_runtime import (
    DockerProcedureSession,
    DockerSecBenchRuntime,
    _FORBIDDEN_TESTCASE_ARTIFACTS,
)
from plugins.security.procedures import (
    CommandTermination,
    ProcedureInfrastructureError,
)
from plugins.security.runtime import docker_cli, image_ensurer, sealer, workspace_mirror


def test_exec_helper_falls_back_to_root_when_project_dirs_disappear(
    tmp_path: Path,
) -> None:
    workspace = SecBenchWorkspace(
        root_id=uuid4(),
        image="secb-tools:demo",
        host_root=tmp_path,
        host_source_dir=tmp_path / "src",
        host_testcase_dir=tmp_path / "testcase",
        host_work_dir=tmp_path / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )
    session = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    runtime = DockerSecBenchRuntime()
    runtime._write_exec_helper(session)

    script = workspace.helper_script.read_text(encoding="utf-8")
    assert 'docker exec "$CONTAINER" test -d /src 2>/dev/null' in script
    assert 'CMD="umask 000; $*"' in script
    assert 'docker exec -i -w / "$CONTAINER" bash -lc "$CMD"' in script


def test_seed_runtime_scripts_replaces_symlinks_without_following(
    tmp_path: Path,
) -> None:
    """Sealed runtime scripts must become regular files, not symlink targets."""
    testcase_dir = tmp_path / "testcase"
    testcase_dir.mkdir()
    outside_repro = tmp_path / "outside-repro"
    outside_patch = tmp_path / "outside-patch"
    outside_repro.write_text("keep repro target", encoding="utf-8")
    outside_patch.write_text("keep patch target", encoding="utf-8")
    (testcase_dir / "repro.sh").symlink_to(outside_repro)
    (testcase_dir / "patch.sh").symlink_to(outside_patch)

    sealer.seed_runtime_scripts(testcase_dir)

    assert outside_repro.read_text(encoding="utf-8") == "keep repro target"
    assert outside_patch.read_text(encoding="utf-8") == "keep patch target"
    assert not (testcase_dir / "repro.sh").is_symlink()
    assert not (testcase_dir / "patch.sh").is_symlink()
    assert "Arise seeded an empty" in (testcase_dir / "repro.sh").read_text(
        encoding="utf-8"
    )
    assert "apply_if_needed /testcase/model_patch.diff" in (
        testcase_dir / "patch.sh"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_start_session_force_removes_container_when_post_run_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit BUG-B: ``docker run`` succeeds and yields a container_id; if a
    post-run setup step (git safe.directory, build.sh chmod, or the exec-helper
    write) raises, the container is already detached and the caller never sees
    it (the session object is never returned). Pre-fix this leaked a running
    container per failed run. Verify the runtime force-removes the container
    before re-raising.
    """
    workspace = SecBenchWorkspace(
        root_id=uuid4(),
        image="secb-tools:demo",
        host_root=tmp_path,
        host_source_dir=tmp_path / "src",
        host_testcase_dir=tmp_path / "testcase",
        host_work_dir=tmp_path / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )
    cve = CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="acme/demo",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="demo bug",
        base_commit="abc123",
        build_sh="#!/bin/bash\nmake\n",
        secb_sh="#!/bin/bash\necho secb\n",
    )

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        # First call is docker run -d ...  → returns a container id.
        if len(invocations) == 1:
            assert cmd[:3] == ["docker", "run", "-d"]
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    def _boom(_session: object) -> None:
        raise RuntimeError("post-run setup boom")

    runtime._write_exec_helper = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="post-run setup boom"):
        await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    # Then: a cleanup docker rm -f was issued against the leaked container.
    rm_invocations = [
        cmd for cmd in invocations if cmd[:4] == ["docker", "rm", "-f", "abc123456789"]
    ]
    assert rm_invocations, (
        f"expected docker rm -f cleanup for the orphaned container, got: {invocations}"
    )


@pytest.mark.asyncio
async def test_start_session_skips_cleanup_when_post_run_setup_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: no rm -f when the setup completes."""
    workspace = SecBenchWorkspace(
        root_id=uuid4(),
        image="secb-tools:demo",
        host_root=tmp_path,
        host_source_dir=tmp_path / "src",
        host_testcase_dir=tmp_path / "testcase",
        host_work_dir=tmp_path / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )
    cve = CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="acme/demo",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="demo bug",
        base_commit="abc123",
    )

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if len(invocations) == 1:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    session = await runtime.start_session(
        cve=cve, workspace=workspace, agent_id=uuid4()
    )

    assert session.container_id == "abc123456789"
    rm_invocations = [
        cmd for cmd in invocations if cmd[:4] == ["docker", "rm", "-f", "abc123456789"]
    ]
    assert rm_invocations == []


def test_sealed_dir_is_outside_the_bind_mounted_run_workspace(tmp_path: Path) -> None:
    # Given: a run workspace root (bind-mounted wholesale at /arise-run).
    run_output = tmp_path / "run-root"
    run_output.mkdir()

    # When: deriving the sealed directory for runtime files (secb-exec, secb).
    sealed = sealer.sealed_dir(run_output)

    # Then: it is a sibling outside the run workspace, so no container bind mount
    # or worker path alias reaches it — a root worker cannot tamper sealed files.
    assert sealed != run_output
    assert run_output.resolve() not in sealed.resolve().parents
    assert sealed.parent == run_output.parent


def _make_workspace(tmp_path: Path, root_id: UUID) -> SecBenchWorkspace:
    src = tmp_path / "src"
    testcase = tmp_path / "testcase"
    work = src / "demo"
    src.mkdir(parents=True, exist_ok=True)
    testcase.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    return SecBenchWorkspace(
        root_id=root_id,
        image="secb-tools:demo",
        host_root=tmp_path,
        host_source_dir=src,
        host_testcase_dir=testcase,
        host_work_dir=work,
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )


def _make_cve() -> CVEInstance:
    return CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="acme/demo",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="demo bug",
        base_commit="abc123",
    )


@pytest.mark.asyncio
async def test_worker_container_name_uses_full_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.3 — worker container name embeds full 32-hex root + agent UUIDs and
    `arise.session_pid` + `arise.created_at` labels appear in the argv."""
    root_id = uuid4()
    agent_id = uuid4()
    workspace = _make_workspace(tmp_path, root_id)
    cve = _make_cve()

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    await runtime.start_session(cve=cve, workspace=workspace, agent_id=agent_id)

    run_cmds = [cmd for cmd in invocations if cmd[:3] == ["docker", "run", "-d"]]
    assert len(run_cmds) == 1
    argv = run_cmds[0]

    expected_name = f"secbench-worker-{root_id.hex}-{agent_id.hex}"
    name_idx = argv.index("--name")
    assert argv[name_idx + 1] == expected_name

    label_pairs = [
        argv[i + 1] for i, token in enumerate(argv) if token == "--label"
    ]
    assert f"arise.session_pid={os.getpid()}" in label_pairs
    assert any(label.startswith("arise.created_at=") for label in label_pairs)
    volume_specs = [argv[i + 1] for i, token in enumerate(argv) if token == "-v"]
    assert any(spec.endswith(":/work") for spec in volume_specs)


@pytest.mark.asyncio
async def test_seed_containers_carry_session_pid_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.3 — seed invocations (source + testcase + work) carry
    the `arise.session_pid` and `arise.role=seed` labels so the F.2 PID-label
    sweep can collect them on SIGKILL."""
    cve = _make_cve()

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return (0, "[]", "")
        if cmd[:2] == ["docker", "create"]:
            return (0, "seedcontainer-id\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    await runtime.prepare_workspace(
        cve=cve,
        run_output_path=tmp_path / "runs" / "root-x",
        image="secb-tools:demo",
        root_id=uuid4(),
    )

    create_cmds = [cmd for cmd in invocations if cmd[:2] == ["docker", "create"]]
    assert len(create_cmds) == 3, f"expected three docker create calls, got {create_cmds}"
    for cmd in create_cmds:
        label_pairs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--label"]
        assert f"arise.session_pid={os.getpid()}" in label_pairs
        assert "arise.role=seed" in label_pairs


@pytest.mark.asyncio
async def test_worker_network_mode_config_default_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.7 — default `--network host` preserved; `network_mode="bridge"`
    threads through the docker run argv."""
    cve = _make_cve()

    captured: list[list[str]] = []

    async def _capture(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        captured.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    # Default → host.
    workspace = _make_workspace(tmp_path / "run-host", uuid4())
    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _capture)
    await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())
    run_argv = next(cmd for cmd in captured if cmd[:3] == ["docker", "run", "-d"])
    net_idx = run_argv.index("--network")
    assert run_argv[net_idx + 1] == "host"

    # Explicit bridge.
    captured.clear()
    workspace = _make_workspace(tmp_path / "run-bridge", uuid4())
    runtime = DockerSecBenchRuntime(network_mode="bridge")
    monkeypatch.setattr(docker_cli, "run_command", _capture)
    await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())
    run_argv = next(cmd for cmd in captured if cmd[:3] == ["docker", "run", "-d"])
    net_idx = run_argv.index("--network")
    assert run_argv[net_idx + 1] == "bridge"


def test_init_raises_on_relative_host_project_root_under_dood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.8 — under DooD (`/.dockerenv` present) an empty/relative
    `HOST_PROJECT_ROOT` must fail fast at construction."""
    real_exists = Path.exists

    def _fake_exists(self: Path) -> bool:
        if str(self) == "/.dockerenv":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _fake_exists)
    monkeypatch.setenv("HOST_PROJECT_ROOT", "")
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    with pytest.raises(RuntimeError, match="HOST_PROJECT_ROOT"):
        DockerSecBenchRuntime()


@pytest.mark.asyncio
async def test_run_command_times_out_on_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.11 — when the subprocess never returns, `_run_command` raises
    `RuntimeError` within roughly the configured timeout window and calls
    `process.kill()`."""

    class _HangingProcess:
        returncode = None

        def __init__(self) -> None:
            self.kill_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()  # blocks forever
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            return 0

    hanging = _HangingProcess()

    async def _fake_create(*_args: object, **_kwargs: object) -> _HangingProcess:
        return hanging

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)

    started = asyncio.get_event_loop().time()
    with pytest.raises(RuntimeError, match="timed out"):
        await docker_cli.run_command(["docker", "sleep", "60"], timeout=0.5)
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed < 1.5, f"timeout took {elapsed:.2f}s, expected < 1.5s"
    assert hanging.kill_called, "expected process.kill() to be called on timeout"


@pytest.mark.asyncio
async def test_run_command_respects_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.11 — the configured `timeout_seconds` is the value passed to
    `asyncio.wait_for`."""

    class _FastProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return 0

    async def _fake_create(*_args: object, **_kwargs: object) -> _FastProcess:
        return _FastProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)

    real_wait_for = asyncio.wait_for
    captured_timeouts: list[float | None] = []

    async def _wait_for_spy(coro: object, timeout: float | None = None) -> object:
        captured_timeouts.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", _wait_for_spy)

    await docker_cli.run_command(["docker", "ps"], timeout=2.0)

    assert 2.0 in captured_timeouts


def test_map_host_work_dir_rejects_traversal(tmp_path: Path) -> None:
    """G.5 — `_map_host_work_dir` normalizes the candidate path and rejects a
    traversal `cve.work_dir` like `/src/../../etc` before any `mkdir`."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()

    with pytest.raises(RuntimeError, match="Refusing work_dir escape"):
        workspace_mirror.map_host_work_dir(source_dir, "/src/../../etc")

    # Sanity: no escape directory landed on the host.
    assert not (tmp_path.parent / "etc").exists()


def test_make_host_tree_writable_summarizes_chmod_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """chmod failures (e.g. EPERM on root-owned files from native-Linux
    `docker cp`) must not abort the walk, but must surface as ONE warning
    instead of vanishing silently."""
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")

    def _deny_chmod(*_args: object) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(Path, "chmod", _deny_chmod)

    with caplog.at_level(logging.WARNING):
        workspace_mirror.make_host_tree_writable(tmp_path)

    assert "Could not adjust permissions on 2 mirrored entries" in caplog.text


@pytest.mark.asyncio
async def test_start_session_rejects_paths_outside_host_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G.5 — `start_session` refuses to mount any workspace path that lives
    outside this run's `host_root`, before any subprocess is invoked."""
    workspace = SecBenchWorkspace(
        root_id=uuid4(),
        image="secb-tools:demo",
        host_root=tmp_path,
        host_source_dir=Path("/etc/passwd"),  # outside host_root
        host_testcase_dir=tmp_path / "testcase",
        host_work_dir=tmp_path / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )
    cve = _make_cve()

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    with pytest.raises(RuntimeError, match="escapes this run's host_root"):
        await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    assert invocations == [], (
        f"expected no docker calls before path validation, got {invocations}"
    )


@pytest.mark.asyncio
async def test_start_session_rejects_sibling_run_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G.5 — sibling-run paths (same pool root, different run_id) are
    rejected. Scope is the run's own `host_root`, not the pool root."""
    pool = tmp_path / "runs"
    pool.mkdir()
    root_a = pool / "root-a"
    root_b = pool / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_b / "src").mkdir()
    (root_a / "testcase").mkdir()
    (root_a / "src").mkdir()
    (root_a / "src" / "demo").mkdir()

    workspace = SecBenchWorkspace(
        root_id=uuid4(),
        image="secb-tools:demo",
        host_root=root_a,
        host_source_dir=root_b / "src",  # sibling run!
        host_testcase_dir=root_a / "testcase",
        host_work_dir=root_a / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=root_a / "secb-exec",
    )
    cve = _make_cve()

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    with pytest.raises(RuntimeError, match="escapes this run's host_root"):
        await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    assert invocations == [], (
        f"expected no docker calls when sibling-run path detected, got {invocations}"
    )


@pytest.mark.asyncio
async def test_ensure_image_pulls_from_registry_on_local_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured registry turns a local miss into a pull + retag to the local
    tag, so a fresh machine runs without building."""

    # Given: the image is absent locally; pull and tag succeed
    invocations: list[list[str]] = []

    async def _fake_run_command(
        cmd: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return (1, "", "No such image")
        return (0, "", "")

    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    # When: ensuring a missing tool image exists
    await image_ensurer.ensure_image_exists(
        "secb-tools:gpac.cve-2023-5586-patch", registry="cheshire0814", timeout=300.0
    )

    # Then: it pulled the namespaced remote and retagged it to the local name
    assert ["docker", "pull", "cheshire0814/secb-tools:gpac.cve-2023-5586-patch"] in invocations
    assert [
        "docker",
        "tag",
        "cheshire0814/secb-tools:gpac.cve-2023-5586-patch",
        "secb-tools:gpac.cve-2023-5586-patch",
    ] in invocations


@pytest.mark.asyncio
async def test_ensure_image_raises_when_registry_pull_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pull falls back to the build-it-yourself error rather than
    masking the miss."""

    # Given: the image is absent locally and the pull fails
    async def _fake_run_command(
        cmd: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]:
        if cmd[:3] == ["docker", "image", "inspect"]:
            return (1, "", "No such image")
        if cmd[:2] == ["docker", "pull"]:
            return (1, "", "manifest unknown")
        return (0, "", "")

    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    # When / Then: ensuring the image raises the build-it-yourself error
    with pytest.raises(RuntimeError, match="Build it first"):
        await image_ensurer.ensure_image_exists(
            "secb-tools:gpac.cve-2023-5586-patch", registry="cheshire0814", timeout=300.0
        )


@pytest.mark.asyncio
async def test_ensure_image_no_registry_does_not_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no registry configured, a local miss raises immediately and never
    attempts a pull (byte-identical to the pre-fallback behavior)."""

    # Given: no registry; the image is absent locally
    invocations: list[list[str]] = []

    async def _fake_run_command(
        cmd: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        return (1, "", "No such image")

    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    # When / Then: it raises without ever pulling
    with pytest.raises(RuntimeError, match="Build it first"):
        await image_ensurer.ensure_image_exists(
            "secb-tools:demo-patch", registry="", timeout=300.0
        )
    assert not any(cmd[:2] == ["docker", "pull"] for cmd in invocations)


@pytest.mark.asyncio
async def test_start_session_seals_delegating_secb_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """secb is overlaid read-only from a sealed source outside every bind mount,
    not written into the container where a root worker shell could rewrite it."""
    # Given: a CVE with no dataset secb_sh and a prepared workspace.
    workspace = _make_workspace(tmp_path, uuid4())
    cve = _make_cve()
    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    # When: starting the worker session.
    await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    # Then: secb is overlaid read-only over /usr/local/bin/secb, with no
    # in-container `cat > /usr/local/bin/secb` install a root worker could repeat.
    run_argv = next(cmd for cmd in invocations if cmd[:3] == ["docker", "run", "-d"])
    volume_specs = [run_argv[i + 1] for i, token in enumerate(run_argv) if token == "-v"]
    secb_spec = next(s for s in volume_specs if s.endswith(":/usr/local/bin/secb:ro"))
    assert not any(
        cmd[:2] == ["docker", "exec"] and "cat > /usr/local/bin/secb" in cmd[-1]
        for cmd in invocations
    )

    # And: the sealed wrapper source is outside the agent-reachable run workspace
    # and carries the non-golden delegating body.
    secb_src = Path(secb_spec.rsplit(":/usr/local/bin/secb:ro", 1)[0])
    assert workspace.host_root.resolve() not in secb_src.resolve().parents
    body = secb_src.read_text(encoding="utf-8")
    # `secb build` must strip REPLAY_ENABLED so compile cannot be diverted to a
    # baked golden replay_build.sh instead of the agent-editable /src/build.sh.
    assert "exec env -u REPLAY_ENABLED /usr/local/bin/compile" in body
    assert "exec /testcase/repro.sh" in body
    assert "exec /testcase/patch.sh" in body


@pytest.mark.asyncio
async def test_start_session_does_not_copy_golden_cve_secb_sh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden secb_sh content must not be installed or exposed to worker containers."""
    # Given: a CVE carrying a golden SEC-bench helper body.
    workspace = _make_workspace(tmp_path, uuid4())
    cve = CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="acme/demo",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="demo bug",
        base_commit="abc123",
        secb_sh="#!/bin/bash\necho GOLDEN-REPRO-SECRET\n",
    )
    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    # When: starting the worker session.
    await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    # Then: no Docker command receives the golden helper body.
    serialized_invocations = "\n".join(" ".join(cmd) for cmd in invocations)
    assert "GOLDEN-REPRO-SECRET" not in serialized_invocations

    # And: the sealed wrapper is Arise-owned (delegating body), never the golden.
    run_argv = next(cmd for cmd in invocations if cmd[:3] == ["docker", "run", "-d"])
    volume_specs = [run_argv[i + 1] for i, token in enumerate(run_argv) if token == "-v"]
    secb_spec = next(s for s in volume_specs if s.endswith(":/usr/local/bin/secb:ro"))
    secb_src = Path(secb_spec.rsplit(":/usr/local/bin/secb:ro", 1)[0])
    body = secb_src.read_text(encoding="utf-8")
    assert "GOLDEN-REPRO-SECRET" not in body
    assert "/usr/local/bin/compile" in body


@pytest.mark.asyncio
async def test_start_session_mounts_patch_script_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime-owned patch helper has no writable container alias."""
    # Given: a prepared workspace with the runtime-owned patch helper.
    workspace = _make_workspace(tmp_path, uuid4())
    (workspace.host_testcase_dir / "patch.sh").write_text(
        "#!/bin/bash\nexit 0\n", encoding="utf-8"
    )
    cve = _make_cve()
    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str], **_kwargs) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(docker_cli, "run_command", _fake_run_command)

    # When: starting the worker session.
    await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    # Then: patch.sh is overlaid as a read-only bind after the writable testcase mount.
    run_argv = next(cmd for cmd in invocations if cmd[:3] == ["docker", "run", "-d"])
    volume_specs = [run_argv[i + 1] for i, token in enumerate(run_argv) if token == "-v"]
    assert any(spec.endswith(":/testcase") for spec in volume_specs)
    assert any(spec.endswith(":/testcase/patch.sh:ro") for spec in volume_specs)
    assert any(spec.endswith(":/arise-run/testcase/patch.sh:ro") for spec in volume_specs)
    testcase_mount_index = next(
        i for i, spec in enumerate(volume_specs) if spec.endswith(":/testcase")
    )
    patch_mount_index = next(
        i for i, spec in enumerate(volume_specs) if spec.endswith(":/testcase/patch.sh:ro")
    )
    workspace_mount_index = next(
        i for i, spec in enumerate(volume_specs) if spec.endswith(":/arise-run")
    )
    patch_alias_mount_index = next(
        i
        for i, spec in enumerate(volume_specs)
        if spec.endswith(":/arise-run/testcase/patch.sh:ro")
    )
    assert patch_mount_index > testcase_mount_index
    assert patch_alias_mount_index > workspace_mount_index


@pytest.mark.asyncio
async def test_prepare_workspace_seeds_non_golden_runtime_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mounted /testcase gets safe repro/patch scripts without deleting PoCs."""
    # Given: copied testcase contents already include a PoC and a golden repro script.
    cve = _make_cve()
    run_dir = tmp_path / "run"
    source_dir = run_dir / "src"
    testcase_dir = run_dir / "testcase"
    work_dir = run_dir / "work"
    source_dir.mkdir(parents=True)
    testcase_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    (source_dir / "demo").mkdir()
    (work_dir / ".keep").write_text("seeded", encoding="utf-8")
    (testcase_dir / "sample.input").write_text("poc bytes", encoding="utf-8")
    (testcase_dir / "repro.sh").write_text(
        "#!/bin/bash\necho GOLDEN-REPRO-SECRET\n", encoding="utf-8"
    )
    for artifact_name in _FORBIDDEN_TESTCASE_ARTIFACTS:
        (testcase_dir / artifact_name).write_text(
            f"forbidden fix artifact: {artifact_name}\n", encoding="utf-8"
        )

    runtime = DockerSecBenchRuntime()
    monkeypatch.setattr(image_ensurer, "ensure_image_exists", AsyncMock())

    # When: preparing the workspace.
    workspace = await runtime.prepare_workspace(
        cve=cve,
        run_output_path=run_dir,
        image="secb-tools:demo-patch",
        root_id=uuid4(),
    )

    # Then: existing PoC-like files remain, but golden repro content is removed.
    assert workspace.host_testcase_dir == testcase_dir
    assert (testcase_dir / "sample.input").read_text(encoding="utf-8") == "poc bytes"
    for artifact_name in _FORBIDDEN_TESTCASE_ARTIFACTS:
        assert not (testcase_dir / artifact_name).exists()
    repro = (testcase_dir / "repro.sh").read_text(encoding="utf-8")
    patch = (testcase_dir / "patch.sh").read_text(encoding="utf-8")
    assert "GOLDEN-REPRO-SECRET" not in repro
    assert "TODO" not in repro
    assert "repo_changes.diff" in patch
    assert "model_patch.diff" in patch
    assert "apply_if_needed /testcase/model_patch.diff" in patch

    # And: the runtime reports the sealed agent-visible surface for event-sourcing.
    sealed = workspace.sealed_surface
    assert sealed is not None
    assert sealed.surface == "secbench"
    by_kind = {a.kind: a for a in sealed.artifacts}
    assert set(by_kind) == {"repro_skeleton", "patch_script", "secb_wrapper"}
    assert by_kind["repro_skeleton"].container_path == "/testcase/repro.sh"
    assert by_kind["patch_script"].container_path == "/testcase/patch.sh"
    assert by_kind["secb_wrapper"].container_path == "/usr/local/bin/secb"
    assert all(a.non_golden and len(a.content_sha256) == 64 for a in sealed.artifacts)


async def test_procedure_session_executes_direct_argv_with_safe_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Procedure commands remain argv vectors and retain the image environment."""

    # Given: a completed Docker exec subprocess and a run container session.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    process_argv: tuple[str, ...] = ()
    process_stdin: bytes | None = None

    class _CompletedProcess:
        returncode = 7

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            nonlocal process_stdin
            process_stdin = input
            return (b"combined output", b"")

        def kill(self) -> None:
            raise AssertionError("completed procedure must not be killed")

    async def _fake_create(*args: str, **_kwargs: object) -> _CompletedProcess:
        nonlocal process_argv
        process_argv = args
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    procedure = DockerProcedureSession(container)

    # When: running arguments that would be shell syntax through a caller environment.
    outcome = await procedure.run(
        ("/work/bin/target", "argument with spaces", ";", "$(touch /tmp/pwned)"),
        timeout=3.5,
        stdin=b"PoC bytes",
        env=(
            ("UBSAN_OPTIONS", "halt_on_error=1"),
            ("PATH", "/tmp/attacker"),
            ("BASH_ENV", "/tmp/payload"),
        ),
    )

    # Then: Docker launches the exact target argv, without a shell or an inner
    # timeout process. The Host owns the deadline.
    target_index = process_argv.index("/work/bin/target")
    assert process_argv[target_index:] == (
        "/work/bin/target",
        "argument with spaces",
        ";",
        "$(touch /tmp/pwned)",
    )
    assert "/usr/bin/timeout" not in process_argv
    assert "bash" not in process_argv[target_index:]
    assert "-lc" not in process_argv

    # And: fixed launch controls cannot be overridden, while sanitizer overrides survive.
    environment = {
        process_argv[index + 1]
        for index, token in enumerate(process_argv)
        if token == "--env"
    }
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in environment
    assert "BASH_ENV=/dev/null" in environment
    assert "ENV=/dev/null" in environment
    assert "LC_ALL=C" in environment
    assert "UBSAN_OPTIONS=halt_on_error=1" in environment
    assert "PATH=/tmp/attacker" not in environment
    assert "BASH_ENV=/tmp/payload" not in environment

    # And: SRC/WORK/OUT/SANITIZER are not replaced, so Docker inherits them from the image.
    assert not any(
        item.startswith(("SRC=", "WORK=", "OUT=", "SANITIZER=")) for item in environment
    )
    assert process_stdin == b"PoC bytes"
    assert outcome.exit_code == 7
    assert outcome.output == "combined output"
    assert not outcome.timed_out


async def test_procedure_session_writes_real_stdin_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a procedure session whose direct argv is replaced with a local byte echo.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    procedure = DockerProcedureSession(container)
    monkeypatch.setattr(
        procedure,
        "_docker_argv",
        lambda _argv, *, env: ["/bin/cat"],
    )

    # When: the Host supplies PoC bytes through stdin.
    outcome = await procedure.run(
        ("/work/bin/target",),
        timeout=1.0,
        stdin=b"exact PoC bytes\x00\n",
    )

    # Then: the real subprocess receives and echoes every byte.
    assert outcome.exit_code == 0
    assert outcome.output.encode("utf-8") == b"exact PoC bytes\x00\n"


async def test_procedure_session_launch_timeout_removes_container_without_waiting_for_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: docker exec launch ignores cancellation after the Host deadline.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    release_launch = asyncio.Event()
    removed = AsyncMock()

    async def _hung_start(
        _docker_argv: list[str],
        *,
        pipe_stdin: bool,
    ) -> asyncio.subprocess.Process:
        assert not pipe_stdin
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            await release_launch.wait()
            raise

    remove = AsyncMock(return_value="")
    procedure = DockerProcedureSession(container, on_removed=removed)
    monkeypatch.setattr(procedure, "_start", _hung_start)
    monkeypatch.setattr(docker_cli, "run_checked", remove)

    # When: launch consumes the command's entire Host deadline.
    outcome = await procedure.run(("/bin/true",), timeout=0.001)

    # Then: the old container is removed before returning a bounded timeout result.
    remove.assert_awaited_once_with(
        ["docker", "rm", "-f", "abc123def456"],
        timeout=60.0,
    )
    removed.assert_awaited_once_with()
    assert outcome.termination is CommandTermination.TIMED_OUT
    assert not procedure.usable

    release_launch.set()
    await asyncio.sleep(0)


async def test_procedure_session_cancellation_during_hung_launch_removes_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a launch coroutine that has started but does not finish when cancelled.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    launch_started = asyncio.Event()
    release_launch = asyncio.Event()
    removed = AsyncMock()

    async def _hung_start(
        _docker_argv: list[str],
        *,
        pipe_stdin: bool,
    ) -> asyncio.subprocess.Process:
        assert not pipe_stdin
        launch_started.set()
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            await release_launch.wait()
            raise

    remove = AsyncMock(return_value="")
    procedure = DockerProcedureSession(container, on_removed=removed)
    monkeypatch.setattr(procedure, "_start", _hung_start)
    monkeypatch.setattr(docker_cli, "run_checked", remove)
    task = asyncio.create_task(procedure.run(("/bin/true",), timeout=30.0))
    await launch_started.wait()

    # When: orchestration cancels the procedure during launch.
    task.cancel()

    # Then: cancellation propagates only after confirmed old-container removal.
    with pytest.raises(asyncio.CancelledError):
        await task
    remove.assert_awaited_once_with(
        ["docker", "rm", "-f", "abc123def456"],
        timeout=60.0,
    )
    removed.assert_awaited_once_with()
    assert not procedure.usable

    release_launch.set()
    await asyncio.sleep(0)


async def test_procedure_session_stops_after_marker_split_across_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    restart = AsyncMock(return_value="abc123def456\n")
    real_wait_for = asyncio.wait_for
    wait_for_timeouts: list[float | None] = []

    class _ChunkedOutput:
        def __init__(self) -> None:
            self._chunks = [
                b"startup\nAvailable flags for Add",
                b"ressSanitizer:\n",
            ]

        async def read(self, _limit: int) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    class _MarkerProcess:
        # Exit 124 is target behavior now; only the Host deadline means timeout.
        returncode: int | None = 124
        stdout = _ChunkedOutput()

        async def communicate(self) -> tuple[bytes, bytes]:
            assert restart.await_count == 1
            return (b"stopped\n", b"")

        async def wait(self) -> int:
            raise AssertionError("a live marker match must reset before waiting for exit")

        def kill(self) -> None:
            raise AssertionError("container restart should reap this docker client")

    process = _MarkerProcess()

    async def _fake_create(*_args: str, **_kwargs: object) -> _MarkerProcess:
        return process

    async def _wait_for_spy(awaitable: object, timeout: float | None) -> object:
        wait_for_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for_spy)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    outcome = await DockerProcedureSession(container).run_until_marker(
        ("/work/bin/target", "--help"),
        marker="Available flags for AddressSanitizer:",
        timeout=0.25,
        env=(("ASAN_OPTIONS", "help=1"),),
    )

    restart.assert_awaited_once_with(
        ["docker", "restart", "--timeout", "0", "abc123def456"],
        timeout=60.0,
    )
    assert outcome.termination is CommandTermination.STOPPED_AFTER_MARKER
    assert not outcome.timed_out
    assert outcome.exit_code == 124
    assert wait_for_timeouts[0] == 0.25
    assert outcome.output == (
        "startup\nAvailable flags for AddressSanitizer:\nstopped\n"
    )


@pytest.mark.parametrize(
    "startup_output",
    [b"Available flags for AddressSan", b"ordinary startup output\n"],
)
async def test_procedure_session_resets_after_completion_without_full_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    startup_output: bytes,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    class _PartialOutput:
        def __init__(self) -> None:
            self._chunks = [startup_output, b""]

        async def read(self, _limit: int) -> bytes:
            return self._chunks.pop(0)

    class _CompletedProcess:
        returncode: int | None = None
        stdout = _PartialOutput()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        async def communicate(self) -> tuple[bytes, bytes]:
            assert restart.await_count == 1
            return (b"", b"")

        def kill(self) -> None:
            raise AssertionError("completed procedure must not be killed")

    async def _fake_create(*_args: str, **_kwargs: object) -> _CompletedProcess:
        return _CompletedProcess()

    restart = AsyncMock(return_value="abc123def456\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    outcome = await DockerProcedureSession(container).run_until_marker(
        ("/work/bin/target", "--help"),
        marker="Available flags for AddressSanitizer:",
        timeout=10.0,
    )

    restart.assert_awaited_once_with(
        ["docker", "restart", "--timeout", "0", "abc123def456"],
        timeout=60.0,
    )
    assert outcome.termination is CommandTermination.COMPLETED
    assert outcome.output == startup_output.decode()


async def test_procedure_session_exit_124_without_full_marker_is_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    class _PartialOutput:
        def __init__(self) -> None:
            self._chunks = [b"Available flags for AddressSan", b""]

        async def read(self, _limit: int) -> bytes:
            return self._chunks.pop(0)

    class _TimedOutProcess:
        returncode: int | None = None
        stdout = _PartialOutput()

        async def wait(self) -> int:
            self.returncode = 124
            return 124

        async def communicate(self) -> tuple[bytes, bytes]:
            assert restart.await_count == 1
            return (b"", b"")

        def kill(self) -> None:
            raise AssertionError("completed procedure must not be killed")

    async def _fake_create(*_args: str, **_kwargs: object) -> _TimedOutProcess:
        return _TimedOutProcess()

    restart = AsyncMock(return_value="abc123def456\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    outcome = await DockerProcedureSession(container).run_until_marker(
        ("/work/bin/target", "--help"),
        marker="Available flags for AddressSanitizer:",
        timeout=10.0,
    )

    restart.assert_awaited_once()
    assert outcome.exit_code == 124
    assert outcome.termination is CommandTermination.COMPLETED
    assert not outcome.timed_out
    assert outcome.output == "Available flags for AddressSan"


async def test_procedure_session_restarts_before_killing_stuck_marker_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    restart = AsyncMock(return_value="abc123def456\n")

    class _MarkerOutput:
        async def read(self, _limit: int) -> bytes:
            return b"Available flags for AddressSanitizer:\n"

    class _StuckClient:
        returncode: int | None = None
        stdout = _MarkerOutput()

        def __init__(self) -> None:
            self.communications = 0
            self.kill_called = False
            self.communication: asyncio.Future[tuple[bytes, bytes]] | None = None

        def communicate(self) -> asyncio.Future[tuple[bytes, bytes]]:
            assert restart.await_count == 1
            self.communications += 1
            assert self.communication is None
            self.communication = asyncio.get_running_loop().create_future()
            return self.communication

        def kill(self) -> None:
            self.kill_called = True
            self.returncode = -9
            assert self.communication is not None
            self.communication.set_result((b"", b""))

    process = _StuckClient()
    cleanup_waits = 0

    async def _fake_create(*_args: str, **_kwargs: object) -> _StuckClient:
        return process

    async def _fake_wait_for(awaitable: object, timeout: float) -> object:
        nonlocal cleanup_waits
        if timeout > 5.0:
            return await awaitable  # type: ignore[misc]
        assert timeout == 5.0
        cleanup_waits += 1
        if cleanup_waits == 1:
            assert isinstance(awaitable, asyncio.Future)
            awaitable.cancel()
            raise TimeoutError
        return await awaitable  # type: ignore[misc]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    outcome = await DockerProcedureSession(container).run_until_marker(
        ("/work/bin/target", "--help"),
        marker="Available flags for AddressSanitizer:",
        timeout=10.0,
    )

    assert process.kill_called
    assert process.communications == 1
    restart.assert_awaited_once()
    assert outcome.termination is CommandTermination.STOPPED_AFTER_MARKER


async def test_procedure_session_marker_restart_failure_is_infrastructure_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    reset_calls: list[list[str]] = []
    process_starts = 0

    class _MarkerOutput:
        async def read(self, _limit: int) -> bytes:
            return b"Available flags for AddressSanitizer:\n"

    class _LiveProcess:
        returncode: int | None = None
        stdout = _MarkerOutput()
        reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            assert reset_calls == [
                ["docker", "restart", "--timeout", "0", "abc123def456"],
                ["docker", "rm", "-f", "abc123def456"],
            ]
            self.returncode = -9
            self.reaped = True
            return (b"", b"")

        def kill(self) -> None:
            raise AssertionError("force-removal should make the client exit")

    async def _fake_create(*_args: str, **_kwargs: object) -> _LiveProcess:
        nonlocal process_starts
        process_starts += 1
        return process

    async def _reset(cmd: list[str], **_kwargs: object) -> str:
        reset_calls.append(cmd)
        if cmd[1] == "restart":
            raise RuntimeError("Docker daemon unavailable")
        return ""

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", _reset)
    process = _LiveProcess()
    procedure = DockerProcedureSession(container)

    with pytest.raises(ProcedureInfrastructureError) as exc_info:
        await procedure.run_until_marker(
            ("/work/bin/target", "--help"),
            marker="Available flags for AddressSanitizer:",
            timeout=10.0,
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert process.reaped
    assert reset_calls == [
        ["docker", "restart", "--timeout", "0", "abc123def456"],
        ["docker", "rm", "-f", "abc123def456"],
    ]

    with pytest.raises(ProcedureInfrastructureError, match="unavailable after a failed reset"):
        await procedure.run(("/bin/true",), timeout=1.0)
    assert process_starts == 1


async def test_procedure_session_normal_exit_124_is_not_a_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target-owned exit 124 is ordinary completion, not Host timeout evidence."""

    # Given: the target itself exits 124 before the Host deadline.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    class _TimedOutProcess:
        returncode = 124

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return (b"partial output", b"")

        def kill(self) -> None:
            raise AssertionError("completed procedure must not be killed")

    async def _fake_create(*_args: str, **_kwargs: object) -> _TimedOutProcess:
        return _TimedOutProcess()

    restart = AsyncMock(return_value="abc123def456\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    # When: the procedure session observes the process status.
    outcome = await DockerProcedureSession(container).run(("/bin/sleep", "60"), timeout=1.0)

    # Then: only the Host watchdog can create TIMED_OUT/reset semantics.
    restart.assert_not_awaited()
    assert outcome.exit_code == 124
    assert outcome.output == "partial output"
    assert outcome.termination is CommandTermination.COMPLETED
    assert not outcome.timed_out


async def test_procedure_session_restarts_container_after_outer_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact Host deadline classifies timeout and resets the container."""

    # Given: the outer watchdog expires before Docker exec returns.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    class _StuckProcess:
        returncode = None

        def __init__(self) -> None:
            self.kill_called = False
            self.communications = 0
            self.communication: asyncio.Future[tuple[bytes, bytes]] | None = None

        def communicate(self, input: bytes | None = None) -> asyncio.Future[tuple[bytes, bytes]]:
            self.communications += 1
            assert self.communication is None
            self.communication = asyncio.get_running_loop().create_future()
            return self.communication

        def kill(self) -> None:
            self.kill_called = True

    process = _StuckProcess()
    wait_calls = 0

    async def _fake_create(*_args: str, **_kwargs: object) -> _StuckProcess:
        return process

    async def _fake_wait_for(awaitable: object, timeout: float) -> object:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            assert timeout == 0.25
            return await awaitable  # type: ignore[misc]
        if wait_calls == 2:
            assert timeout <= 0.25
            assert isinstance(awaitable, asyncio.Future)
            awaitable.cancel()
            raise TimeoutError
        assert isinstance(awaitable, asyncio.Future)
        return await awaitable

    async def _restart(*_args: object, **_kwargs: object) -> str:
        assert process.communication is not None
        process.returncode = 143
        process.communication.set_result((b"partial output", b""))
        return "abc123def456\n"

    restart = AsyncMock(side_effect=_restart)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    # When: running a command past the host watchdog.
    outcome = await DockerProcedureSession(container).run(("/bin/sleep", "60"), timeout=0.25)

    # Then: restart stops the workload and the Docker client is reaped afterward.
    assert process.communications == 1
    assert not process.kill_called
    restart.assert_awaited_once_with(
        ["docker", "restart", "--timeout", "0", "abc123def456"],
        timeout=60.0,
    )
    assert outcome.exit_code == 124
    assert outcome.output == "partial output"
    assert outcome.timed_out


async def test_procedure_session_timeout_preserves_real_stream_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation of the watchdog wrapper must not cancel the output reader."""
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    procedure = DockerProcedureSession(container)
    process: asyncio.subprocess.Process | None = None

    async def _start(
        _docker_argv: list[str],
        *,
        pipe_stdin: bool,
    ) -> asyncio.subprocess.Process:
        nonlocal process
        assert not pipe_stdin
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            "printf 'before deadline\\n'; exec /bin/sleep 60",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return process

    async def _restart(*_args: object, **_kwargs: object) -> str:
        assert process is not None
        process.terminate()
        return "abc123def456\n"

    monkeypatch.setattr(procedure, "_start", _start)
    monkeypatch.setattr(docker_cli, "run_checked", AsyncMock(side_effect=_restart))

    outcome = await procedure.run(("/bin/ignored",), timeout=0.25)

    assert outcome.termination is CommandTermination.TIMED_OUT
    assert outcome.output == "before deadline\n"


async def test_procedure_session_marker_after_deadline_does_not_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bytes observed only while reaping cannot satisfy the expired predicate."""
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    restart = AsyncMock(return_value="abc123def456\n")

    class _LateOutput:
        def __init__(self) -> None:
            self._reads = 0

        async def read(self, _limit: int) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return b"startup before deadline\n"
            await asyncio.get_running_loop().create_future()
            raise AssertionError("unreachable")

    class _LateMarkerProcess:
        returncode: int | None = None
        stdout = _LateOutput()

        async def communicate(self) -> tuple[bytes, bytes]:
            assert restart.await_count == 1
            self.returncode = 143
            return (b"Available flags for AddressSanitizer:\n", b"")

        def kill(self) -> None:
            raise AssertionError("container reset should reap this client")

    async def _fake_create(*_args: str, **_kwargs: object) -> _LateMarkerProcess:
        return _LateMarkerProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", restart)

    outcome = await DockerProcedureSession(container).run_until_marker(
        ("/work/bin/target", "--help"),
        marker="Available flags for AddressSanitizer:",
        timeout=0.001,
    )

    restart.assert_awaited_once()
    assert outcome.termination is CommandTermination.TIMED_OUT
    assert outcome.timed_out
    assert outcome.output == "startup before deadline\n"
    assert "Available flags for AddressSanitizer:" not in outcome.output


async def test_procedure_session_cancellation_resets_and_reaps_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    first_communication_started = asyncio.Event()
    communication_finished: asyncio.Future[tuple[bytes, bytes]] = (
        asyncio.get_running_loop().create_future()
    )

    class _CancellableProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.communications = 0
            self.reaped = False

        async def communicate(
            self, input: bytes | None = None
        ) -> tuple[bytes, bytes]:
            self.communications += 1
            assert self.communications == 1
            first_communication_started.set()
            result = await communication_finished
            self.returncode = 143
            self.reaped = True
            return result

        def kill(self) -> None:
            raise AssertionError("container reset should reap this client")

    process = _CancellableProcess()

    async def _restart(*_args: object, **_kwargs: object) -> str:
        communication_finished.set_result((b"", b""))
        return "abc123def456\n"

    restart = AsyncMock(side_effect=_restart)

    async def _fake_create(*_args: str, **_kwargs: object) -> _CancellableProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", restart)
    procedure = DockerProcedureSession(container)

    task = asyncio.create_task(procedure.run(("/bin/sleep", "60"), timeout=30.0))
    await first_communication_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    restart.assert_awaited_once_with(
        ["docker", "restart", "--timeout", "0", "abc123def456"],
        timeout=60.0,
    )
    assert process.communications == 1
    assert process.reaped


async def test_unreapable_client_is_removed_before_session_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: reset succeeds but its docker exec client ignores kill and two reap waits.
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    events: list[str] = []
    communication: asyncio.Future[tuple[bytes, bytes]] = (
        asyncio.get_running_loop().create_future()
    )

    class _MarkerOutput:
        async def read(self, _limit: int) -> bytes:
            return b"Available flags for AddressSanitizer:\n"

    class _UnreapableProcess:
        returncode: int | None = None
        stdout = _MarkerOutput()

        def communicate(self) -> asyncio.Future[tuple[bytes, bytes]]:
            return communication

        def kill(self) -> None:
            events.append("kill-client")

        async def wait(self) -> int:
            events.append("wait-client")
            self.returncode = -9
            return -9

    async def _fake_create(*_args: str, **_kwargs: object) -> _UnreapableProcess:
        return _UnreapableProcess()

    async def _docker_command(cmd: list[str], **_kwargs: object) -> str:
        events.append("restart" if cmd[1] == "restart" else "remove")
        return ""

    async def _removed() -> None:
        events.append("invalidate")

    cleanup_waits = 0
    real_wait_for = asyncio.wait_for

    async def _wait_for(awaitable: object, timeout: float) -> object:
        nonlocal cleanup_waits
        if timeout > 5.0:
            return await real_wait_for(awaitable, timeout)
        cleanup_waits += 1
        if cleanup_waits <= 2:
            assert isinstance(awaitable, asyncio.Future)
            awaitable.cancel()
            raise TimeoutError
        return await awaitable  # type: ignore[misc]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)
    monkeypatch.setattr(docker_cli, "run_checked", _docker_command)
    procedure = DockerProcedureSession(container, on_removed=_removed)

    # When: marker success triggers reset but the local client cannot be reaped.
    with pytest.raises(ProcedureInfrastructureError, match="container was removed"):
        await procedure.run_until_marker(
            ("/work/bin/target", "--help"),
            marker="Available flags for AddressSanitizer:",
            timeout=10.0,
        )

    # Then: force-removal and client settlement precede cache invalidation.
    assert events == [
        "restart",
        "kill-client",
        "remove",
        "kill-client",
        "wait-client",
        "invalidate",
    ]
    assert communication.cancelled()
    assert not procedure.usable


async def test_procedure_session_oserror_during_reset_poisons_and_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _make_workspace(tmp_path, uuid4())
    container = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )
    reset_calls: list[list[str]] = []
    removed = AsyncMock()

    class _MarkerOutput:
        async def read(self, _limit: int) -> bytes:
            return b"Available flags for AddressSanitizer:\n"

    class _LiveProcess:
        returncode: int | None = None
        stdout = _MarkerOutput()
        reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            self.returncode = 143
            self.reaped = True
            return (b"", b"")

        def kill(self) -> None:
            raise AssertionError("failed reset cleanup should reap this client")

    process = _LiveProcess()

    async def _fake_create(*_args: str, **_kwargs: object) -> _LiveProcess:
        return process

    async def _reset(cmd: list[str], **_kwargs: object) -> str:
        reset_calls.append(cmd)
        raise OSError("subprocess launch failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(docker_cli, "run_checked", _reset)
    procedure = DockerProcedureSession(container, on_removed=removed)

    with pytest.raises(ProcedureInfrastructureError) as exc_info:
        await procedure.run_until_marker(
            ("/work/bin/target", "--help"),
            marker="Available flags for AddressSanitizer:",
            timeout=10.0,
        )

    assert isinstance(exc_info.value.__cause__, OSError)
    assert process.reaped
    removed.assert_not_awaited()
    assert not procedure.usable
    assert reset_calls == [
        ["docker", "restart", "--timeout", "0", "abc123def456"],
        ["docker", "rm", "-f", "abc123def456"],
    ]
    with pytest.raises(ProcedureInfrastructureError, match="unavailable after a failed reset"):
        await procedure.run(("/bin/true",), timeout=1.0)
