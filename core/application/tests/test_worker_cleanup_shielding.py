"""Test that worker cleanup does not block CancelledError propagation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cancellation_propagates_despite_slow_cleanup() -> None:
    """When a worker task is cancelled, CancelledError must propagate
    immediately even if _cleanup_worker_context would block.
    """
    from core.application.services.lifecycle.role_dispatch import (
        WorkerHandler,
        DispatchContext,
    )

    # Given: a mock context where cleanup hangs for 60s
    context = MagicMock(spec=DispatchContext)
    context.worker_semaphore = asyncio.Semaphore(1)
    context.worker_semaphore_holders = set()
    context.get_agent_depth = MagicMock(return_value=1)
    context.get_root_id = MagicMock(return_value=MagicMock())
    context.sibling_view_port = AsyncMock()
    context.domain_plugin = MagicMock()
    context.get_workspace_context = MagicMock(return_value=None)
    context.get_working_directory = MagicMock(return_value="/tmp")
    context.get_run_output_path = MagicMock(return_value=None)
    # execute_task must block long enough for us to cancel
    async def blocking_execute(*args, **kwargs):
        await asyncio.sleep(60)

    context.orchestrator = AsyncMock()
    context.orchestrator.execute_task = blocking_execute

    # Cleanup blocks for 60s — should NOT delay cancellation
    async def slow_cleanup(**kwargs):
        await asyncio.sleep(60)

    context.domain_plugin.cleanup_worker_execution = slow_cleanup
    context.domain_plugin.prepare_worker_execution = AsyncMock(return_value=None)

    handler = WorkerHandler(context)

    agent = MagicMock()
    agent.agent_id = MagicMock()
    agent.parent_id = MagicMock()
    agent.role = MagicMock()
    agent.role.value = "worker"
    agent.status = MagicMock()
    agent.status.value = "analyzing"
    agent.hierarchy_limits = None

    # When: we run the handler and cancel after 0.1s
    task = asyncio.create_task(handler._execute(agent))
    await asyncio.sleep(0.1)
    task.cancel()

    # Then: CancelledError propagates within 2s, not 60s
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
