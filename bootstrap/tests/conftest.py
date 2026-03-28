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

from bootstrap import (
    ApplicationConfig,
    InfrastructureConfig,
    get_application,
    get_infrastructure,
)
from config import BossConfig, ConcurrencyConfig, ManagerConfig, ToolCallingConfig, TopologyConfig
from core.domain.events.events import (
    CodeGenerationStarted,
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
)
from core.ports.runtime_ports import LLMPort
from core.ports.runtime_ports import WorkerToolPort
from presentation.cli import CLI, CLIConfig


def _create_test_cli(
    infrastructure_config: InfrastructureConfig,
    cli_config: CLIConfig | None = None,
) -> CLI:
    """Create a fully wired CLI for E2E tests with default configs."""
    infra = get_infrastructure(infrastructure_config)

    # Default configs for testing
    boss_config = BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
    manager_config = ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)

    app_config = ApplicationConfig(
        topology=TopologyConfig(
            max_depth=5,
            max_children_per_node=10,
            max_total_agents=100,
        ),
        concurrency=ConcurrencyConfig(max_concurrent_workers=5),
        tool_calling=ToolCallingConfig(),
        max_retries=3,
        poll_interval=0.5,
        boss_config=boss_config,
        manager_config=manager_config,
        output_directory="./output",
        default_worker_tool=infrastructure_config.default_worker_tool,
    )

    app = get_application(infra, app_config)
    return CLI(execution_service=app.execution_service, config=cli_config)


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

        agent_id = task_context.get("agent_id", UUID(int=0))

        # Emit start event
        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=0,
            tool_name="fake-tool",
        )

        # Emit some thinking
        yield ThoughtCaptured(
            aggregate_id=agent_id,
            sequence_number=1,
            content="Fake worker executing task...",
            stream="stdout",
        )

        # Emit completion
        yield WorkCompleted(
            aggregate_id=agent_id,
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
    return InfrastructureConfig(
        postgres_connection_string=e2e_postgres_url,
        default_worker_tool="claude_code",
        worker_tool_model="claude-sonnet-4-20250514",
        worker_tool_timeout=300,
    )


@pytest.fixture
def e2e_cli(infrastructure_config: InfrastructureConfig) -> CLI:
    """Provide fully wired CLI for E2E tests.

    Note: This uses real PostgreSQL but fake LLM/worker for deterministic tests.
    For true E2E with real LLM, set appropriate API keys.
    """
    cli_config = CLIConfig(verbose=False)
    return _create_test_cli(
        infrastructure_config=infrastructure_config,
        cli_config=cli_config,
    )
