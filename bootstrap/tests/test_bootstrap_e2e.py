"""End-to-end tests for the bootstrap layer.

Tests verify dependency injection wiring works correctly.

Run with:
    uv run pytest bootstrap/tests/ -v
"""

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from bootstrap import (
    ApplicationConfig,
    InfrastructureConfig,
    get_application,
    get_cli,
    get_infrastructure,
)
from config import ConcurrencyConfig, RetryConfig, ToolCallingConfig, TopologyConfig
from presentation.cli import CLI, CLIConfig, RunResult


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

    def test_passes_openhands_iteration_limit_into_adapter(self) -> None:
        """OpenHands adapter should receive the configured per-run iteration cap."""
        infra = get_infrastructure(
            make_infra_config(
                default_worker_tool="openhands",
                worker_tool_max_iterations=7,
            )
        )

        assert getattr(infra.worker_tool, "max_iterations_per_run", None) == 7

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

    def test_wires_retry_config_into_execution_service(self) -> None:
        """Retry settings from ApplicationConfig must be visible to ExecutionService."""
        infra = get_infrastructure(make_infra_config())
        retry_cfg = RetryConfig(
            model_escalation_chain=["openai/gpt-4o"],
            no_progress_max_retries=3,
        )
        app = get_application(infra, make_app_config(retry=retry_cfg))

        assert app.execution_service._retry_config is not None
        assert app.execution_service._retry_config.no_progress_max_retries == 3


class TestPresentationWiring:
    """Tests for presentation layer factory."""

    def test_get_cli_returns_cli_instance(self) -> None:
        """Test that get_cli returns a properly configured CLI."""
        infra = get_infrastructure(make_infra_config())
        app = get_application(infra, make_app_config())
        cli = get_cli(app.execution_service, infra.event_store, CLIConfig(verbose=False))

        assert isinstance(cli, CLI)
        assert cli.config.verbose is False

    def test_cli_receives_execution_service(self) -> None:
        """Test that CLI has access to execution service."""
        infra = get_infrastructure(make_infra_config())
        app = get_application(infra, make_app_config())
        cli = get_cli(app.execution_service, infra.event_store)

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
        cli = get_cli(app.execution_service, infra.event_store, CLIConfig(
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


class TestRunTaskManifestAssembly:
    """Verify bootstrap._run_task wiring around cli.run_task() and manifest write.

    These tests exercise the orchestration contract documented in spec §10
    without spinning up Postgres: they stub out CLI construction, the event
    store context, and projection pipeline so the assertions focus on the
    three guarantees that motivated Phase 1:

    (a) wall_started_at is captured BEFORE cli.run_task() is awaited;
    (b) a FRESH event store is opened for projection (not shared with CLI);
    (c) write_run_manifest receives RunResult.status unchanged.
    """

    @pytest.mark.asyncio
    async def test_run_task_manifest_inputs_are_plumbed_correctly(
        self,
        tmp_path: Any,
    ) -> None:
        from bootstrap import bootstrap as bootstrap_module

        # Given: a deterministic run_id returned by a faked CLI.
        run_id = uuid4()

        # And: instrumentation to capture the observed timeline.
        timeline: list[tuple[str, datetime]] = []
        captured_manifest_kwargs: dict[str, Any] = {}

        async def _fake_cli_run_task(
            task: str,  # noqa: ARG001 - matches CLI.run_task signature
            domain_context: object | None = None,  # noqa: ARG001 - matches CLI.run_task signature
        ) -> RunResult:
            # Record the moment cli.run_task() starts executing so the test
            # can assert wall_started_at was stamped BEFORE this point.
            timeline.append(("cli_run_task_entered", datetime.now(UTC)))
            await asyncio.sleep(0)  # let the event loop tick
            return RunResult(root_id=run_id, status="timeout")

        fake_cli = MagicMock()
        fake_cli.run_task = AsyncMock(side_effect=_fake_cli_run_task)

        # Event store contexts opened by bootstrap — bootstrap should open
        # its own, separate from anything CLI used internally.
        opened_stores: list[MagicMock] = []

        @asynccontextmanager
        async def _fake_event_store_cm(_settings: Any) -> AsyncIterator[MagicMock]:
            store = MagicMock(name=f"EventStore-{len(opened_stores)}")
            opened_stores.append(store)
            yield store

        # Projection pipeline returns a synthetic summary the manifest writer
        # will receive unchanged.
        fake_summary = MagicMock(name="ProjectionSummary")
        fake_pipeline = MagicMock()
        fake_pipeline.execute_summary = AsyncMock(return_value=fake_summary)

        fake_builder = MagicMock()
        fake_builder.with_output.return_value = fake_builder
        fake_builder.build.return_value = fake_pipeline

        # Manifest writer: capture kwargs so the test can inspect the
        # forwarded fields.
        async def _fake_write_run_manifest(run_id_arg: Any, **kwargs: Any) -> Any:
            timeline.append(("write_run_manifest_entered", datetime.now(UTC)))
            captured_manifest_kwargs["run_id"] = run_id_arg
            captured_manifest_kwargs.update(kwargs)
            return tmp_path / "manifest.json"

        fake_persistence = MagicMock()
        fake_persistence.write_run_manifest = AsyncMock(
            side_effect=_fake_write_run_manifest
        )

        # Settings stub — only the attributes bootstrap reaches into matter.
        settings_stub = MagicMock(name="Settings")
        settings_stub.output.verbose = False
        settings_stub.output.directory = str(tmp_path)

        args = argparse.Namespace(
            config=None,
            task="smoke-task",
            domain_context_file=None,
            domain=None,
            worker_model=None,
            worker_tool=None,
        )

        # When: running _run_task with every collaborator stubbed.
        with (
            patch.object(bootstrap_module, "_load_settings", return_value=settings_stub),
            patch.object(
                bootstrap_module, "_apply_worker_overrides", side_effect=lambda s, _: s
            ),
            patch.object(
                bootstrap_module,
                "get_run_domain_components",
                return_value=MagicMock(plugin=None),
            ),
            patch.object(bootstrap_module, "create_runtime_cli", return_value=fake_cli),
            patch.object(bootstrap_module, "_event_store", _fake_event_store_cm),
            patch(
                "core.query.projections.ProjectionPipelineBuilder",
                return_value=fake_builder,
            ),
            patch(
                "presentation.persistence.RunPersistence",
                return_value=fake_persistence,
            ),
        ):
            # Snapshot before delegation — anything captured by bootstrap as
            # wall_started_at must be >= this instant but < the moment
            # cli.run_task() started executing.
            test_start = datetime.now(UTC)
            # Non-success runs exit non-zero (contract with automation); the
            # manifest writer still fires before sys.exit, so the captured
            # kwargs are populated below.
            with pytest.raises(SystemExit):
                await bootstrap_module._run_task(args)

        # Then:

        # (c) the manifest received the RunResult.status verbatim — bootstrap
        # did not re-infer anything.
        assert captured_manifest_kwargs["run_id"] == run_id
        assert captured_manifest_kwargs["exit_status"] == "timeout"
        assert captured_manifest_kwargs["summary"] is fake_summary

        # (a) wall_started_at was captured BEFORE cli.run_task() was awaited.
        wall_started = captured_manifest_kwargs["wall_started_at"]
        wall_ended = captured_manifest_kwargs["wall_ended_at"]
        cli_entered_at = next(
            moment for name, moment in timeline if name == "cli_run_task_entered"
        )
        assert test_start <= wall_started <= cli_entered_at
        assert wall_started < wall_ended

        # (b) a fresh event store was opened for projection. We count exactly
        # one bootstrap-owned store per _run_task invocation; separation from
        # any CLI-internal store is by construction since create_runtime_cli
        # is also patched out.
        assert len(opened_stores) == 1
        # And: the projection pipeline was driven against that store.
        fake_pipeline.execute_summary.assert_awaited_once_with(root_agent_id=run_id)

        # Sanity: the stubbed CLI.run_task was actually invoked with the args
        # bootstrap forwarded — proves the create_runtime_cli patch is live
        # and the other assertions are not passing spuriously.
        fake_cli.run_task.assert_awaited_once_with("smoke-task", domain_context=None)
