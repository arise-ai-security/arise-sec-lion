from __future__ import annotations

import asyncio
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
from plugins.security.docker_runtime import DockerSecBenchRuntime


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


@pytest.mark.asyncio
async def test_start_session_force_removes_container_when_secb_install_fails(
    tmp_path: Path,
) -> None:
    """Audit BUG-B: ``docker run`` succeeds and yields a container_id; if a
    post-run setup step (``_install_secb``, ``_add_git_safe_directory``,
    chmod) raises, the container is already detached and the caller never
    sees it (the session object is never returned). Pre-fix this leaked a
    running container per failed run. Verify the runtime force-removes the
    container before re-raising.
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

    async def _fake_run_command(cmd: list[str]) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        # First call is docker run -d ...  → returns a container id.
        if len(invocations) == 1:
            assert cmd[:3] == ["docker", "run", "-d"]
            return (0, "abc1234567890\n", "")
        # Second call is _install_secb's docker exec ... bash -lc ...
        if cmd[:2] == ["docker", "exec"] and "bash" in cmd:
            return (1, "", "secb install boom")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    runtime._run_command = _fake_run_command  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="secb install boom"):
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

    async def _fake_run_command(cmd: list[str]) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if len(invocations) == 1:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    runtime._run_command = _fake_run_command  # type: ignore[method-assign]

    session = await runtime.start_session(
        cve=cve, workspace=workspace, agent_id=uuid4()
    )

    assert session.container_id == "abc123456789"
    rm_invocations = [
        cmd for cmd in invocations if cmd[:4] == ["docker", "rm", "-f", "abc123456789"]
    ]
    assert rm_invocations == []


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
async def test_worker_container_name_uses_full_uuid(tmp_path: Path) -> None:
    """E.3 — worker container name embeds full 32-hex root + agent UUIDs and
    `arise.session_pid` + `arise.created_at` labels appear in the argv."""
    root_id = uuid4()
    agent_id = uuid4()
    workspace = _make_workspace(tmp_path, root_id)
    cve = _make_cve()

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str]) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    runtime._run_command = _fake_run_command  # type: ignore[method-assign]

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
async def test_seed_containers_carry_session_pid_label(tmp_path: Path) -> None:
    """E.3 — seed invocations (source + testcase + work) carry
    the `arise.session_pid` and `arise.role=seed` labels so the F.2 PID-label
    sweep can collect them on SIGKILL."""
    cve = _make_cve()

    invocations: list[list[str]] = []

    async def _fake_run_command(cmd: list[str]) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return (0, "[]", "")
        if cmd[:2] == ["docker", "create"]:
            return (0, "seedcontainer-id\n", "")
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    runtime._run_command = _fake_run_command  # type: ignore[method-assign]

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
async def test_worker_network_mode_config_default_host(tmp_path: Path) -> None:
    """E.7 — default `--network host` preserved; `network_mode="bridge"`
    threads through the docker run argv."""
    cve = _make_cve()

    captured: list[list[str]] = []

    async def _capture(cmd: list[str]) -> tuple[int, str, str]:
        captured.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            return (0, "abc1234567890\n", "")
        return (0, "", "")

    # Default → host.
    workspace = _make_workspace(tmp_path / "run-host", uuid4())
    runtime = DockerSecBenchRuntime()
    runtime._run_command = _capture  # type: ignore[method-assign]
    await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())
    run_argv = next(cmd for cmd in captured if cmd[:3] == ["docker", "run", "-d"])
    net_idx = run_argv.index("--network")
    assert run_argv[net_idx + 1] == "host"

    # Explicit bridge.
    captured.clear()
    workspace = _make_workspace(tmp_path / "run-bridge", uuid4())
    runtime = DockerSecBenchRuntime(network_mode="bridge")
    runtime._run_command = _capture  # type: ignore[method-assign]
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

    runtime = DockerSecBenchRuntime(timeout_seconds=0.5)

    started = asyncio.get_event_loop().time()
    with pytest.raises(RuntimeError, match="timed out"):
        await runtime._run_command(["docker", "sleep", "60"])
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

    runtime = DockerSecBenchRuntime(timeout_seconds=2.0)
    await runtime._run_command(["docker", "ps"])

    assert 2.0 in captured_timeouts


def test_map_host_work_dir_rejects_traversal(tmp_path: Path) -> None:
    """G.5 — `_map_host_work_dir` normalizes the candidate path and rejects a
    traversal `cve.work_dir` like `/src/../../etc` before any `mkdir`."""
    runtime = DockerSecBenchRuntime()
    source_dir = tmp_path / "src"
    source_dir.mkdir()

    with pytest.raises(RuntimeError, match="Refusing work_dir escape"):
        runtime._map_host_work_dir(source_dir, "/src/../../etc")

    # Sanity: no escape directory landed on the host.
    assert not (tmp_path.parent / "etc").exists()


@pytest.mark.asyncio
async def test_start_session_rejects_paths_outside_host_root(tmp_path: Path) -> None:
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

    async def _fake_run_command(cmd: list[str]) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    runtime._run_command = _fake_run_command  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="escapes this run's host_root"):
        await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    assert invocations == [], (
        f"expected no docker calls before path validation, got {invocations}"
    )


@pytest.mark.asyncio
async def test_start_session_rejects_sibling_run_paths(tmp_path: Path) -> None:
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

    async def _fake_run_command(cmd: list[str]) -> tuple[int, str, str]:
        invocations.append(list(cmd))
        return (0, "", "")

    runtime = DockerSecBenchRuntime()
    runtime._run_command = _fake_run_command  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="escapes this run's host_root"):
        await runtime.start_session(cve=cve, workspace=workspace, agent_id=uuid4())

    assert invocations == [], (
        f"expected no docker calls when sibling-run path detected, got {invocations}"
    )
