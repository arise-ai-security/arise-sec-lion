"""Integration test: step timeout must produce failure events even when cleanup blocks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.execution_service import AgentExecutionService, ServiceConfig


@pytest.mark.asyncio
async def test_step_timeout_marks_agent_failed() -> None:
    """When a worker step exceeds step_timeout_seconds, the agent must be
    marked FAILED in the DB regardless of cleanup behavior.

    This is the exact scenario that caused 9% of workers to hang:
    step timeout fires -> CancelledError -> cleanup blocks -> no failure events.
    """
    # Given: a service with a very short step timeout
    config = MagicMock(spec=ServiceConfig)
    config.step_timeout_seconds = 0.5  # 500ms
    config.worker_silence_timeout_seconds = 600.0
    config.stall_timeout_seconds = 600.0
    config.poll_interval = 0.1
    config.max_retries = 3
    config.llm_jitter_max_ms = 0

    agent_id = MagicMock()
    mock_agent = MagicMock()
    mock_agent.agent_id = agent_id
    mock_agent.role.value = "worker"
    mock_agent.status.value = "analyzing"
    mock_agent.version = 5
    mock_agent.is_terminal.return_value = False

    repository = AsyncMock()
    repository.load_if_exists = AsyncMock(return_value=mock_agent)

    service = AgentExecutionService.__new__(AgentExecutionService)
    service._config = config
    service._repository = repository
    service._progress_callback = None
    service._parent_notifier = AsyncMock()
    service._llm_semaphore_waiters = set()
    service._llm_semaphore_holders = set()
    service._task_started_at = {}
    service._llm_jitter_max_ms = 0

    # The step hangs forever (simulating a stuck worker)
    async def hang_forever(aid):
        await asyncio.sleep(3600)

    service.run_agent_step = hang_forever

    # When: step runs with skip_llm_semaphore (worker path)
    service._task_started_at[agent_id] = 0
    await service._run_step_with_semaphore(agent_id, skip_llm_semaphore=True)

    # Then: agent was marked failed via _handle_step_timeout
    mock_agent.fail_with_reason.assert_called_once()
    assert "timed out" in mock_agent.fail_with_reason.call_args[0][0].lower()
    repository.persist_events.assert_called()
