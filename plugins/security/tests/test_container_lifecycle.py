from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.plugin import SecurityDomainPlugin


def _sample_cve() -> CVEInstance:
    return CVEInstance(
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


@pytest.mark.asyncio
async def test_security_plugin_prepares_workspace_and_session(tmp_path: Path) -> None:
    root_id = uuid4()
    agent_id = uuid4()
    cve = _sample_cve()

    workspace = SecBenchWorkspace(
        root_id=root_id,
        image="secb-tools:demo.cve-2024-0001",
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

    runtime = AsyncMock()
    runtime.prepare_workspace.return_value = workspace
    runtime.start_session.return_value = session

    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind", "klee"],
        container_runtime=runtime,
    )

    prepared = await plugin.prepare_run(
        root_id=root_id,
        run_output_path=tmp_path,
        domain_context=cve,
    )
    worker_context = await plugin.prepare_worker_execution(
        root_id=root_id,
        agent_id=agent_id,
        run_output_path=tmp_path,
        domain_context=cve,
    )

    assert prepared is not None
    assert prepared.working_directory == str(tmp_path)
    runtime.prepare_workspace.assert_awaited_once()
    assert (
        runtime.prepare_workspace.await_args.kwargs["image"]
        == "secb-tools:demo.cve-2024-0001-patch"
    )

    assert worker_context is not None
    container_session = worker_context.task_context["container_session"]
    assert container_session["container_id"] == "abc123def456"
    assert container_session["host_source_dir"] == str(tmp_path / "src")

    mcp_servers = worker_context.task_context["mcp_servers"]
    assert "security_tools" in mcp_servers
    security_tools_spec = mcp_servers["security_tools"]
    assert security_tools_spec["args"] == [
        "-m",
        "plugins.security.mcp.security_tools_server",
    ]
    assert security_tools_spec["env"]["ARISE_SECBENCH_CONTAINER_ID"] == "abc123def456"

    await plugin.cleanup_worker_execution(
        root_id=root_id,
        agent_id=agent_id,
        domain_context=cve,
    )
    runtime.stop_session.assert_awaited_once_with(session)
