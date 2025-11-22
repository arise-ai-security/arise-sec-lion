"""Fixtures for bootstrap layer E2E tests.

These fixtures provide the fully wired application for end-to-end testing.
E2E tests require real infrastructure (PostgreSQL, optionally LLM APIs).

Requirements:
    - PostgreSQL running (docker compose up -d postgres)
    - Environment variables set for API keys (optional, can use fakes)
"""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from bootstrap import InfrastructureConfig, bootstrap
from core.domain.events import (
    CodeGenerationStarted,
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
)
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort
from presentation.cli import CLI, CLIConfig


# ============================================================================
# Test Doubles for External Services
# ============================================================================


class FakeLLMPort(LLMPort):
    """Fake LLM for E2E tests without real API calls.

    Returns predetermined responses based on prompt content.
    """

    def __init__(self) -> None:
        """Initialize with response templates."""
        self.call_count = 0
        self.prompts: list[str] = []

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Return fake LLM responses based on prompt content."""
        self.call_count += 1
        self.prompts.append(prompt)

        # Complexity evaluation response (for PENDING agents)
        if "complexity" in prompt.lower() and "evaluate" in prompt.lower():
            return '{"complexity": "simple", "reasoning": "Task is straightforward"}'

        # Task decomposition response (for BOSS/MANAGER agents)
        if "decompose" in prompt.lower() or "subtask" in prompt.lower():
            return """[
                {"description": "Subtask 1: Initialize project structure"},
                {"description": "Subtask 2: Implement core feature"}
            ]"""

        # Default response
        return '{"result": "completed"}'


class FakeWorkerToolPort(WorkerToolPort):
    """Fake worker tool for E2E tests without real tool execution."""

    def __init__(self) -> None:
        """Initialize with execution tracking."""
        self.executions: list[dict[str, Any]] = []

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Simulate worker tool execution with fake events."""
        self.executions.append(task_context)

        session_id = task_context.get("session_id", UUID(int=0))

        # Emit start event
        yield CodeGenerationStarted(
            aggregate_id=session_id,
            sequence_number=0,
            tool_name="fake-tool",
        )

        # Emit some thinking
        yield ThoughtCaptured(
            aggregate_id=session_id,
            sequence_number=1,
            content="Fake worker executing task...",
            stream="stdout",
        )

        # Emit completion
        yield WorkCompleted(
            aggregate_id=session_id,
            sequence_number=2,
            result="Task completed successfully by fake worker",
        )


# ============================================================================
# Pytest Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def e2e_postgres_url() -> str:
    """Get PostgreSQL URL for E2E tests.

    Uses environment variable or defaults to local Docker instance.
    """
    return os.environ.get(
        "E2E_POSTGRES_URL",
        "postgresql://arise:arise@localhost:5432/arise_events_test",
    )


@pytest.fixture
def fake_llm() -> FakeLLMPort:
    """Provide fake LLM for E2E tests."""
    return FakeLLMPort()


@pytest.fixture
def fake_worker_tool() -> FakeWorkerToolPort:
    """Provide fake worker tool for E2E tests."""
    return FakeWorkerToolPort()


@pytest.fixture
def infrastructure_config(e2e_postgres_url: str) -> InfrastructureConfig:
    """Provide infrastructure config for E2E tests."""
    return InfrastructureConfig(postgres_connection_string=e2e_postgres_url)


@pytest.fixture
def e2e_cli(infrastructure_config: InfrastructureConfig) -> CLI:
    """Provide fully wired CLI for E2E tests.

    Note: This uses real PostgreSQL but fake LLM/worker for deterministic tests.
    For true E2E with real LLM, set appropriate API keys.
    """
    cli_config = CLIConfig(verbose=False)
    return bootstrap(
        infrastructure_config=infrastructure_config,
        cli_config=cli_config,
    )
