"""End-to-end tests for the bootstrap layer.

Tests verify dependency injection wiring works correctly.

Run with:
    uv run pytest bootstrap/tests/ -v
"""

import pytest

from bootstrap import (
    ApplicationConfig,
    InfrastructureConfig,
    get_application,
    get_cli,
    get_infrastructure,
)
from config import ConcurrencyConfig, ToolCallingConfig, TopologyConfig
from presentation.cli import CLI, CLIConfig


pytestmark = pytest.mark.e2e


def make_infra_config(**overrides) -> InfrastructureConfig:
    """Create InfrastructureConfig with sensible defaults."""
    defaults = {
        "postgres_connection_string": "postgresql://test:test@localhost:5432/test",
        "default_worker_tool": "claude_code",
        "worker_tool_model": "openai/gpt-4o",
        "worker_tool_timeout": 300,
    }
    return InfrastructureConfig(**(defaults | overrides))


def make_app_config(**overrides) -> ApplicationConfig:
    """Create ApplicationConfig with sensible defaults."""
    from config import BossConfig, ManagerConfig

    defaults = {
        "topology": TopologyConfig(
            max_depth=-1, max_children_per_node=-1, max_total_agents=-1,
        ),
        "concurrency": ConcurrencyConfig(max_concurrent_workers=-1),
        "tool_calling": ToolCallingConfig(),
        "max_retries": 3,
        "poll_interval": 0.5,
        "boss_config": BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        "manager_config": ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        "output_directory": "./test_output",
        "default_worker_tool": "claude_code",
    }
    return ApplicationConfig(**(defaults | overrides))


class TestInfrastructureWiring:
    """Tests for infrastructure layer factory."""

    def test_creates_all_adapters(self) -> None:
        """Test that get_infrastructure creates all required adapters."""
        infra = get_infrastructure(make_infra_config())

        assert infra.event_store is not None
        assert infra.llm_adapter is not None
        assert infra.worker_tool is not None

    def test_config_requires_all_fields(self) -> None:
        """Test that InfrastructureConfig validates required fields."""
        with pytest.raises(TypeError, match=r"missing.*required"):
            InfrastructureConfig(  # type: ignore[call-arg]
                postgres_connection_string="postgresql://test:test@localhost:5432/test"
            )


class TestApplicationWiring:
    """Tests for application layer factory."""

    def test_creates_execution_service(self) -> None:
        """Test that get_application creates execution service."""
        infra = get_infrastructure(make_infra_config())
        app = get_application(infra, make_app_config())

        assert app.execution_service is not None


class TestPresentationWiring:
    """Tests for presentation layer factory."""

    def test_get_cli_returns_cli_instance(self) -> None:
        """Test that get_cli returns a properly configured CLI."""
        infra = get_infrastructure(make_infra_config())
        app = get_application(infra, make_app_config())
        cli = get_cli(app.execution_service, CLIConfig(verbose=False))

        assert isinstance(cli, CLI)
        assert cli.config.verbose is False

    def test_cli_receives_execution_service(self) -> None:
        """Test that CLI has access to execution service."""
        infra = get_infrastructure(make_infra_config())
        app = get_application(infra, make_app_config())
        cli = get_cli(app.execution_service)

        assert cli.execution_service is app.execution_service


class TestFullWiringChain:
    """Tests for complete wiring from infrastructure to presentation."""

    def test_full_wiring_chain(self) -> None:
        """Test complete dependency injection chain."""
        # Infrastructure
        infra = get_infrastructure(make_infra_config(
            default_worker_tool="openhands",
            worker_tool_timeout=600,
        ))

        # Application
        app = get_application(infra, make_app_config(
            max_retries=5,
            poll_interval=1.0,
        ))

        # Presentation
        cli = get_cli(app.execution_service, CLIConfig(
            verbose=True,
            output_directory="./custom_output",
        ))

        assert cli.config.verbose is True
        assert cli.config.output_directory == "./custom_output"


@pytest.mark.skipif(
    "not config.getoption('--run-e2e', default=False)",
    reason="E2E tests require --run-e2e flag and running PostgreSQL",
)
class TestFullAgentFlow:
    """E2E tests requiring PostgreSQL.

    Run with: uv run pytest bootstrap/tests/ --run-e2e -v
    """

    @pytest.mark.asyncio
    async def test_boss_agent_creation_and_persistence(self, e2e_cli: CLI) -> None:
        """Test that BOSS agent can be created and persisted."""
        await e2e_cli.execution_service.initialize()
        try:
            boss_id = await e2e_cli.execution_service.create_boss_agent(
                task_description="Test task for E2E"
            )
            result = await e2e_cli.execution_service.get_agent_result(boss_id)
            assert result.role == "boss"
            assert result.task_description == "Test task for E2E"
        finally:
            await e2e_cli.execution_service.cleanup()

    @pytest.mark.asyncio
    async def test_system_statistics_after_creation(self, e2e_cli: CLI) -> None:
        """Test that system statistics reflect created agents."""
        await e2e_cli.execution_service.initialize()
        try:
            await e2e_cli.execution_service.create_boss_agent(task_description="Stats test")
            stats = await e2e_cli.execution_service.get_system_statistics()
            assert stats.total_agents >= 1
        finally:
            await e2e_cli.execution_service.cleanup()
