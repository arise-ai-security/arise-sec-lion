from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from plugins.security.cve_instance import CVEInstance
from plugins.security.docker_runtime import DockerSecBenchRuntime
from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)


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
