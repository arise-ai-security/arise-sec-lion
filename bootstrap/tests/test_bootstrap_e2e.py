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
from config import OrchestrationConfig
from presentation.cli import CLI, CLIConfig


# Mark all tests in this module as e2e
pytestmark = pytest.mark.e2e


def make_test_system_limits() -> OrchestrationConfig.LimitsConfig:
    """Create test system limits (all unlimited for tests)."""
    return OrchestrationConfig.LimitsConfig(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
        llm_rate_limit_rpm=-1,
    )


def make_test_infra_config(
    postgres_connection_string: str = "postgresql://test:test@localhost:5432/test",
    default_worker_tool: str = "claude_code",
    worker_tool_model: str = "openai/gpt-4o",
    worker_tool_timeout: int = 300,
) -> InfrastructureConfig:
    """Create a complete InfrastructureConfig for testing."""
    return InfrastructureConfig(
        postgres_connection_string=postgres_connection_string,
        default_worker_tool=default_worker_tool,
        worker_tool_model=worker_tool_model,
        worker_tool_timeout=worker_tool_timeout,
    )


def make_test_app_config(
    system_limits: OrchestrationConfig.LimitsConfig | None = None,
    max_retries: int = 3,
    poll_interval: float = 0.5,
    model_config: dict[str, str] | None = None,
    output_directory: str = "./test_output",
    default_worker_tool: str = "claude_code",
    budget_max_total_cost_usd: float = 10.0,
    budget_cost_warning_threshold: float = 0.8,
    budget_cost_tracking_enabled: bool = True,
) -> ApplicationConfig:
    """Create a complete ApplicationConfig for testing."""
    return ApplicationConfig(
        system_limits=system_limits or make_test_system_limits(),
        max_retries=max_retries,
        poll_interval=poll_interval,
        model_config=model_config or {"boss": "gpt-4o"},
        output_directory=output_directory,
        default_worker_tool=default_worker_tool,
        budget_max_total_cost_usd=budget_max_total_cost_usd,
        budget_cost_warning_threshold=budget_cost_warning_threshold,
        budget_cost_tracking_enabled=budget_cost_tracking_enabled,
    )


class TestBootstrapWiring:
    """Tests for bootstrap dependency injection."""

    def test_bootstrap_returns_cli(self) -> None:
        """Test that bootstrap returns a CLI instance."""
        cli = bootstrap(
            infrastructure_config=make_test_infra_config(),
            application_config=make_test_app_config(),
            cli_config=CLIConfig(verbose=False, output_directory="./test_output"),
        )
        assert isinstance(cli, CLI)

    def test_bootstrap_with_custom_config(self) -> None:
        """Test that bootstrap accepts custom configurations."""
        infra_config = make_test_infra_config(
            postgres_connection_string="postgresql://custom:custom@localhost:5432/custom",
            default_worker_tool="openhands",
            worker_tool_model="anthropic/claude-3-5-sonnet",
            worker_tool_timeout=600,
        )
        app_config = make_test_app_config(
            max_retries=5,
            poll_interval=1.0,
            model_config={
                "boss": "gpt-4",
                "manager": "gpt-4",
                "worker": "gpt-4",
                "pending": "gpt-4",
            },
        )
        cli_config = CLIConfig(verbose=True, output_directory="./test_output")

        cli = bootstrap(
            infrastructure_config=infra_config,
            application_config=app_config,
            cli_config=cli_config,
        )

        assert cli.config.verbose is True

    def test_bootstrap_uses_config_file_defaults(self, monkeypatch) -> None:
        """Test that bootstrap loads config from default config file."""
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")

        cli = bootstrap()
        assert isinstance(cli, CLI)


class TestInfrastructureWiring:
    """Tests for infrastructure layer wiring."""

    def test_get_infrastructure_creates_adapters(self) -> None:
        """Test that infrastructure factory creates all adapters."""
        config = make_test_infra_config()
        infra = get_infrastructure(config)

        assert infra.event_store is not None
        assert infra.llm_adapter is not None
        assert infra.worker_tool is not None

    def test_infrastructure_config_requires_all_fields(self) -> None:
        """Test that InfrastructureConfig raises error if fields are missing."""
        with pytest.raises(TypeError, match=r"missing.*required"):
            InfrastructureConfig(  # type: ignore[call-arg]
                postgres_connection_string="postgresql://test:test@localhost:5432/test"
            )


class TestApplicationWiring:
    """Tests for application layer wiring."""

    def test_get_application_creates_service(self) -> None:
        """Test that application factory creates execution service."""
        infra = get_infrastructure(make_test_infra_config())
        app_config = make_test_app_config()
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
    async def test_system_statistics_after_creation(
        self,
        e2e_cli: CLI,
    ) -> None:
        """Test that system statistics reflect created agents."""
        await e2e_cli.execution_service.initialize()

        try:
            await e2e_cli.execution_service.create_boss_agent(task_description="Stats test task")

            stats = await e2e_cli.execution_service.get_system_statistics()
            assert stats.total_agents >= 1

        finally:
            await e2e_cli.execution_service.cleanup()


class TestConfigurationLoading:
    """Tests for configuration file loading."""

    def test_bootstrap_with_yaml_config_path(self, tmp_path, monkeypatch) -> None:
        """Test that bootstrap can load config from YAML file."""
        monkeypatch.setenv("POSTGRES_PASSWORD", "yaml")

        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
database:
  host: localhost
  port: 5432
  user: yaml
  name: yaml

llm:
  model_boss: gpt-4o

worker:
  tool_type: claude_code
  tool_model: openai/gpt-4o
  tool_timeout: 300

orchestration:
  max_retries: 10
  retry_delay: 0.1
  poll_interval: 2.0
  llm_timeout: 30.0
  worker_timeout: 300.0
  default_task_complexity_threshold: 5
  budget:
    max_total_cost_usd: 10.0
    max_tokens_per_agent: 100000
    cost_warning_threshold: 0.8
    cost_tracking_enabled: true
  limits:
    max_depth: -1
    max_children_per_node: -1
    max_total_agents: -1
    max_concurrent_workers: -1
    llm_rate_limit_rpm: -1

output:
  verbose: false
  show_progress: true
  log_level: INFO
  directory: ./output
""")

        cli = bootstrap(config_path=config_file)
        assert cli.config.verbose is False
