"""End-to-end tests for the complete multi-agent system.

These tests verify the fully wired application from bootstrap to execution.
They require PostgreSQL to be running (use docker compose up -d postgres).

Test Categories:
    - Bootstrap wiring: Verify dependency injection works correctly
    - Full flow: Verify complete agent lifecycle from BOSS to WORKER
    - Error handling: Verify system handles failures gracefully

Run with:
    uv run pytest bootstrap/tests/ -v

Skip if no database:
    uv run pytest bootstrap/tests/ -v -m "not e2e"
"""

import pytest

from bootstrap import ApplicationConfig, InfrastructureConfig, bootstrap
from bootstrap.bootstrap import get_application, get_infrastructure
from presentation.cli import CLI, CLIConfig


# Mark all tests in this module as e2e
pytestmark = pytest.mark.e2e


class TestBootstrapWiring:
    """Tests for bootstrap dependency injection."""

    def test_bootstrap_returns_cli(self) -> None:
        """Test that bootstrap returns a CLI instance."""
        cli = bootstrap(
            infrastructure_config=InfrastructureConfig(
                postgres_connection_string="postgresql://test:test@localhost:5432/test"
            ),
            cli_config=CLIConfig(verbose=False),
        )
        assert isinstance(cli, CLI)

    def test_bootstrap_with_custom_config(self) -> None:
        """Test that bootstrap accepts custom configurations."""
        infra_config = InfrastructureConfig(
            postgres_connection_string="postgresql://custom:custom@localhost:5432/custom"
        )
        app_config = ApplicationConfig(
            max_retries=5,
            poll_interval=1.0,
            model_config={
                "boss": "gpt-4",
                "manager": "gpt-4",
                "worker": "gpt-4",
                "pending": "gpt-4",
            },
        )
        cli_config = CLIConfig(verbose=True)

        cli = bootstrap(
            infrastructure_config=infra_config,
            application_config=app_config,
            cli_config=cli_config,
        )

        assert cli.config.verbose is True

    def test_bootstrap_uses_defaults_when_no_config(self) -> None:
        """Test that bootstrap uses defaults when no config provided."""
        cli = bootstrap()
        assert isinstance(cli, CLI)
        assert cli.config.verbose is True  # Default


class TestInfrastructureWiring:
    """Tests for infrastructure layer wiring."""

    def test_get_infrastructure_creates_adapters(self) -> None:
        """Test that infrastructure factory creates all adapters."""
        config = InfrastructureConfig(
            postgres_connection_string="postgresql://test:test@localhost:5432/test"
        )
        infra = get_infrastructure(config)

        assert infra.event_store is not None
        assert infra.llm_adapter is not None
        assert infra.worker_tool is not None


class TestApplicationWiring:
    """Tests for application layer wiring."""

    def test_get_application_creates_service(self) -> None:
        """Test that application factory creates execution service."""
        infra_config = InfrastructureConfig(
            postgres_connection_string="postgresql://test:test@localhost:5432/test"
        )
        infra = get_infrastructure(infra_config)

        app_config = ApplicationConfig(max_retries=3, poll_interval=0.5)
        app = get_application(infra, app_config)

        assert app.execution_service is not None


@pytest.mark.skipif(
    "not config.getoption('--run-e2e', default=False)",
    reason="E2E tests require --run-e2e flag and running PostgreSQL",
)
class TestFullAgentFlow:
    """E2E tests for complete agent execution flow.

    These tests require:
    1. PostgreSQL running (docker compose up -d postgres)
    2. Run with: uv run pytest bootstrap/tests/ --run-e2e -v

    Note: Uses fake LLM/worker for deterministic testing.
    For true E2E with real LLM, set OPENAI_API_KEY or ANTHROPIC_API_KEY.
    """

    @pytest.mark.asyncio
    async def test_boss_agent_creation_and_persistence(
        self,
        e2e_cli: CLI,
    ) -> None:
        """Test that BOSS agent can be created and persisted."""
        # Initialize infrastructure
        await e2e_cli.execution_service.initialize()

        try:
            # Create BOSS agent
            boss_id = await e2e_cli.execution_service.create_boss_agent(
                task_description="Test task for E2E"
            )

            # Verify agent exists
            result = await e2e_cli.execution_service.get_agent_result(boss_id)
            assert result.role == "boss"
            assert result.task_description == "Test task for E2E"

        finally:
            await e2e_cli.execution_service.cleanup()

    @pytest.mark.asyncio
    async def test_system_statistics_after_creation(
        self,
        e2e_cli: CLI,
    ) -> None:
        """Test that system statistics reflect created agents."""
        await e2e_cli.execution_service.initialize()

        try:
            # Create BOSS agent
            await e2e_cli.execution_service.create_boss_agent(task_description="Stats test task")

            # Get statistics
            stats = await e2e_cli.execution_service.get_system_statistics()
            assert stats.total_agents >= 1

        finally:
            await e2e_cli.execution_service.cleanup()


class TestConfigurationLoading:
    """Tests for configuration file loading."""

    def test_bootstrap_with_yaml_config_path(self, tmp_path) -> None:
        """Test that bootstrap can load config from YAML file."""
        # Create a test config file
        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
infrastructure:
  postgres_connection_string: "postgresql://yaml:yaml@localhost:5432/yaml"

application:
  max_retries: 10
  poll_interval: 2.0

presentation:
  verbose: false
""")

        cli = bootstrap(config_path=config_file)
        assert cli.config.verbose is False
