import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.ports.domain_plugin_port import WorkspacePathAlias
from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.docker_runtime import DockerProcedureSession
from plugins.security.plugin import SecurityDomainPlugin
from plugins.security.procedures import ProcedureInfrastructureError


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
    assert prepared.path_aliases == (
        WorkspacePathAlias("/src", str(tmp_path / "src")),
        WorkspacePathAlias("/testcase", str(tmp_path / "testcase")),
        WorkspacePathAlias("/work", str(tmp_path / "work")),
    )
    runtime.prepare_workspace.assert_awaited_once()
    assert (
        runtime.prepare_workspace.await_args.kwargs["image"]
        == "secb-tools:demo.cve-2024-0001-patch"
    )

    assert worker_context is not None
    container_session = worker_context.task_context["container_session"]
    assert container_session["container_id"] == "abc123def456"
    assert container_session["host_source_dir"] == str(tmp_path / "src")
    assert container_session["host_work_root"] == str(tmp_path / "work")
    assert container_session["container_work_dir"] == "/work"

    mcp_servers = worker_context.task_context["mcp_servers"]
    assert "security_tools" in mcp_servers
    security_tools_spec = mcp_servers["security_tools"]
    assert security_tools_spec["args"] == [
        "-m",
        "plugins.security.mcp.security_tools_server",
    ]
    assert security_tools_spec["env"]["ARISE_SECBENCH_CONTAINER_ID"] == "abc123def456"
    assert security_tools_spec["env"]["ARISE_SECBENCH_HOST_SOURCE_DIR"] == str(
        tmp_path / "src"
    )
    assert security_tools_spec["env"]["ARISE_SECBENCH_HOST_TESTCASE_DIR"] == str(
        tmp_path / "testcase"
    )
    assert security_tools_spec["env"]["ARISE_SECBENCH_HOST_WORK_ROOT"] == str(
        tmp_path / "work"
    )
    assert security_tools_spec["env"]["ARISE_SECBENCH_CONTAINER_WORK_DIR"] == "/work"

    # Per-worker cleanup is a no-op (one shared container per run, kept until
    # process exit) — the container is NOT stopped here.
    await plugin.cleanup_worker_execution(
        root_id=root_id,
        agent_id=agent_id,
        domain_context=cve,
    )


@pytest.mark.asyncio
async def test_run_workers_share_one_container_by_default(
    tmp_path: Path,
) -> None:
    """A run's workers share ONE container BY DEFAULT (no flag).

    The second worker's prepare REUSES the existing session (same container_id,
    no second start_session), and per-worker cleanup does NOT stop the container
    — it persists for the whole run so files worker 1 wrote to non-mounted
    container paths survive for worker 2.
    """
    root_id = uuid4()
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
        container_id="shared0container",
        container_name="secbench-worker-shared",
        image=workspace.image,
    )
    runtime = AsyncMock()
    runtime.prepare_workspace.return_value = workspace
    runtime.start_session.return_value = session

    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind"],
        container_runtime=runtime,
    )
    await plugin.prepare_run(root_id=root_id, run_output_path=tmp_path, domain_context=cve)

    # Two workers, sequentially (as the scheduler dispatches them).
    ctx1 = await plugin.prepare_worker_execution(
        root_id=root_id, agent_id=uuid4(), run_output_path=tmp_path, domain_context=cve
    )
    await plugin.cleanup_worker_execution(root_id=root_id, agent_id=uuid4(), domain_context=cve)
    ctx2 = await plugin.prepare_worker_execution(
        root_id=root_id, agent_id=uuid4(), run_output_path=tmp_path, domain_context=cve
    )

    # One container, reused (same id); start_session called exactly once.
    assert runtime.start_session.await_count == 1
    assert ctx1 is not None and ctx2 is not None
    assert ctx1.task_context["container_session"]["container_id"] == "shared0container"
    assert ctx2.task_context["container_session"]["container_id"] == "shared0container"

    # The cached adapter owns the lock that serializes Host commands in this container.
    first_procedure_session = plugin._resolve_procedure_session(root_id)
    second_procedure_session = plugin._resolve_procedure_session(root_id)
    assert first_procedure_session is not None
    assert first_procedure_session is second_procedure_session


@pytest.mark.asyncio
async def test_blocked_procedure_session_is_replaced_only_after_confirmed_removal(
    tmp_path: Path,
) -> None:
    root_id = uuid4()
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
    first_session = SecBenchContainerSession(
        workspace=workspace,
        container_id="first0000001",
        container_name="secbench-worker-first",
        image=workspace.image,
    )
    replacement_session = SecBenchContainerSession(
        workspace=workspace,
        container_id="second000001",
        container_name="secbench-worker-second",
        image=workspace.image,
    )
    runtime = AsyncMock()
    runtime.prepare_workspace.return_value = workspace
    runtime.start_session.side_effect = [first_session, replacement_session]
    plugin = SecurityDomainPlugin(container_runtime=runtime)
    await plugin.prepare_run(root_id=root_id, run_output_path=tmp_path, domain_context=cve)

    await plugin.prepare_worker_execution(
        root_id=root_id,
        agent_id=uuid4(),
        run_output_path=tmp_path,
        domain_context=cve,
    )
    poisoned = plugin._resolve_procedure_session(root_id)
    assert isinstance(poisoned, DockerProcedureSession)

    poisoned._usable = False

    with pytest.raises(ProcedureInfrastructureError, match="removal was not confirmed"):
        await plugin.prepare_worker_execution(
            root_id=root_id,
            agent_id=uuid4(),
            run_output_path=tmp_path,
            domain_context=cve,
        )
    assert runtime.start_session.await_count == 1
    assert plugin._resolve_procedure_session(root_id) is poisoned

    await poisoned._confirm_removed()

    assert plugin._resolve_procedure_session(root_id) is None
    fallback = await plugin.prepare_worker_execution(
        root_id=root_id,
        agent_id=uuid4(),
        run_output_path=tmp_path,
        domain_context=cve,
    )
    assert fallback is not None
    assert fallback.task_context["container_session"]["container_id"] == "second000001"
    assert runtime.start_session.await_count == 2
    replacement = plugin._resolve_procedure_session(root_id)
    assert isinstance(replacement, DockerProcedureSession)
    assert replacement is not poisoned


@pytest.mark.asyncio
async def test_prepare_worker_execution_serializes_concurrent_calls_per_root(
    tmp_path: Path,
) -> None:
    """Audit §13#9: two concurrent prepare_worker_execution calls for the
    same root_id MUST NOT both reach start_session. Pre-fix the check
    ``if root_id in self._sessions`` and the assignment
    ``self._sessions[root_id] = session`` were not under any lock, so two
    coroutines could both pass the membership check and race to create
    two containers for the same run.
    """
    root_id = uuid4()
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

    start_calls = 0
    in_flight = 0
    max_in_flight = 0

    async def _start_session(**_kwargs) -> SecBenchContainerSession:
        nonlocal start_calls, in_flight, max_in_flight
        start_calls += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # Yield long enough that an unlocked second caller could win the
        # check-and-set race if the lock were missing.
        await asyncio.sleep(0.05)
        in_flight -= 1
        return SecBenchContainerSession(
            workspace=workspace,
            container_id=f"abc{start_calls:08x}",
            container_name=f"secbench-worker-{start_calls}",
            image=workspace.image,
        )

    runtime = AsyncMock()
    runtime.prepare_workspace.return_value = workspace
    runtime.start_session.side_effect = _start_session

    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind"],
        container_runtime=runtime,
    )
    await plugin.prepare_run(
        root_id=root_id,
        run_output_path=tmp_path,
        domain_context=cve,
    )

    results = await asyncio.gather(
        plugin.prepare_worker_execution(
            root_id=root_id,
            agent_id=uuid4(),
            run_output_path=tmp_path,
            domain_context=cve,
        ),
        plugin.prepare_worker_execution(
            root_id=root_id,
            agent_id=uuid4(),
            run_output_path=tmp_path,
            domain_context=cve,
        ),
        return_exceptions=True,
    )

    # Exactly one start_session call: the second coroutine sees the session
    # already in the dict under the lock and REUSES it (one shared container by
    # default) — it must not race a second container into existence.
    assert start_calls == 1
    # Never more than one concurrent start_session under the lock.
    assert max_in_flight == 1
    # Both succeed: one starts the container, the other reuses it.
    successes = [r for r in results if not isinstance(r, BaseException)]
    assert len(successes) == 2
    assert all(
        r.task_context["container_session"]["container_id"] == "abc00000001" for r in successes
    )
