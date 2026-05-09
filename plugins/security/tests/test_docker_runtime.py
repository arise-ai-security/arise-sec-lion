from pathlib import Path
from uuid import uuid4

import pytest

from plugins.security.docker_runtime import DockerSecBenchRuntime
from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
from plugins.security.cve_instance import CVEInstance


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
async def test_start_session_removes_stale_same_name_container_before_run(
    tmp_path: Path,
) -> None:
    root_id = uuid4()
    agent_id = uuid4()
    workspace = SecBenchWorkspace(
        root_id=root_id,
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
    workspace.host_source_dir.mkdir(parents=True, exist_ok=True)
    workspace.host_testcase_dir.mkdir(parents=True, exist_ok=True)
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
        secb_sh="",
    )

    runtime = DockerSecBenchRuntime()
    calls: list[list[str]] = []
    inspect_calls = 0
    stale_removed = False

    async def _fake_run_command(
        cmd: list[str], timeout: float | None = None,
    ) -> tuple[int, str, str]:
        nonlocal inspect_calls, stale_removed
        del timeout
        calls.append(cmd)
        if cmd[:3] == ["docker", "container", "inspect"]:
            inspect_calls += 1
            if stale_removed and inspect_calls >= 3:
                return 1, "", "No such container"
            return 0, "{}", ""
        if cmd[:3] == ["docker", "rm", "-f"]:
            stale_removed = True
            return 0, "", ""
        if cmd[:3] == ["docker", "run", "-d"]:
            return 0, "60426023abcdef\n", ""
        if cmd[:2] == ["docker", "exec"]:
            return 0, "", ""
        return 0, "", ""

    runtime._run_command = _fake_run_command  # type: ignore[method-assign]
    session = await runtime.start_session(cve=cve, workspace=workspace, agent_id=agent_id)

    expected_name = f"secbench-worker-{root_id.hex[:8]}-{agent_id.hex[:8]}"
    inspect_idx = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "container", "inspect"])
    rm_idx = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"])
    run_idx = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "run", "-d"])

    assert calls[inspect_idx][-1] == expected_name
    assert calls[rm_idx][-1] == expected_name
    assert inspect_idx < rm_idx < run_idx
    assert session.container_name == expected_name
