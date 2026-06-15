from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from bootstrap.application import ExecutionLimitsBridge
from config import BossConfig, ManagerConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    HierarchyLimitsRegistry,
    ServiceConfig,
)
from core.application.services import (
    AgentQueryService,
    AgentRepository,
    ChildAgentFactory,
    ParentNotificationService,
    PromptBuilder,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import (
    AgentCreated,
    CodeGenerationStarted,
    ComplexityEvaluated,
    RuntimeSurfaceSealed,
    TaskAssigned,
    WorkCompleted,
)
from core.domain.values.node_message import Handoff
from core.ports.domain_plugin_port import (
    PreparedRunWorkspace,
    SealedRuntimeArtifact,
    SealedRuntimeSurface,
    WorkerExecutionContext,
    WorkspacePathAlias,
)


def _test_config() -> dict[str, Any]:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


def _test_system_limits() -> ExecutionLimitsBridge:
    return ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
    )


class StubDomainPlugin:
    def __init__(self, working_directory: str) -> None:
        self.working_directory = working_directory
        self.prepare_worker_execution = AsyncMock(
            return_value=WorkerExecutionContext(
                working_directory=working_directory,
                task_context={"container_session": {"container_id": "abc123"}},
            )
        )
        self.cleanup_worker_execution = AsyncMock()

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        return None

    def get_run_metadata(self, domain_context: object) -> dict[str, Any]:
        return {}

    def get_tag_mappings(self) -> dict[str, Any]:
        return {}

    def get_provenance_patterns(self) -> list[tuple[str, Any]]:
        return []

    async def prepare_run(
        self,
        *,
        root_id,
        run_output_path,
        domain_context,
    ):
        return None


class AsyncWorkerPort:
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.called = False
        self.last_task_context = None

    def run_session(self, task_context: dict[str, Any]):
        self.called = True
        self.last_task_context = task_context
        return self

    def __aiter__(self):
        self._events = iter(
            [
                CodeGenerationStarted(
                    aggregate_id=self.agent_id,
                    sequence_number=4,
                    tool_name="claude_code",
                ),
                WorkCompleted(
                    aggregate_id=self.agent_id,
                    sequence_number=5,
                    result="done",
                ),
            ]
        )
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class CapturingReconTool:
    def __init__(self) -> None:
        self.working_directory: str | None = None
        self.path_aliases: tuple[WorkspacePathAlias, ...] | None = None

    def set_working_directory(self, path: str) -> None:
        self.working_directory = path

    def set_path_aliases(self, aliases: tuple[WorkspacePathAlias, ...]) -> None:
        self.path_aliases = aliases


@pytest.mark.asyncio
async def test_setup_working_directory_configures_recon_path_aliases(
    tmp_path: Path,
) -> None:
    # Given:
    root_id = uuid4()
    run_root = tmp_path / "run-root"
    aliases = (
        WorkspacePathAlias("/src", str(run_root / "src")),
        WorkspacePathAlias("/testcase", str(run_root / "testcase")),
    )
    domain_plugin = AsyncMock()
    domain_plugin.prepare_run.return_value = PreparedRunWorkspace(
        working_directory=str(run_root),
        path_aliases=aliases,
    )
    recon_tool = CapturingReconTool()
    service = object.__new__(AgentExecutionService)
    service._config = ServiceConfig(
        max_retries=3,
        poll_interval=0.5,
        output_directory=str(tmp_path),
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )
    service._domain_plugin = domain_plugin
    service._recon_tool = recon_tool
    service._working_directory = None

    # When:
    await service._setup_working_directory(root_id, domain_context=None)

    # Then:
    assert service._working_directory == run_root
    assert recon_tool.working_directory == str(run_root)
    assert recon_tool.path_aliases == aliases
    domain_plugin.prepare_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_execution_includes_domain_plugin_task_context(tmp_path: Path) -> None:
    agent_id = uuid4()
    mock_event_store = AsyncMock()
    mock_llm_port = AsyncMock()
    worker_port = AsyncWorkerPort(agent_id)
    limits_registry = HierarchyLimitsRegistry()
    limits_registry.create_root(
        agent_id,
        max_depth=-1,
        max_children_per_node=-1,
        max_retries=3,
    )

    boss_config = BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
    manager_config = ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.5,
        output_directory=str(tmp_path),
        default_worker_tool="claude_code",
        boss_config=boss_config,
        manager_config=manager_config,
    )

    prompt_builder = PromptBuilder("prompts", "claude_code")
    repository = AgentRepository(event_store=mock_event_store, max_retries=3)
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=manager_config,
    )
    orchestrator = AgentOrchestrator(
        llm_port=mock_llm_port,
        worker_port=worker_port,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
    )

    sibling_view_mock = AsyncMock()
    sibling_view_mock.build_view.return_value = Handoff(
        parent_task=None,
        siblings=(),
        shared_decisions=(),
    )
    domain_plugin = StubDomainPlugin(str(tmp_path))
    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=AsyncMock(),
        sibling_view_port=sibling_view_mock,
        parent_notifier=ParentNotificationService(repository=repository),
        prompt_builder=prompt_builder,
        domain_plugin=domain_plugin,
    )
    execution_service = AgentExecutionService(
        event_store=mock_event_store,
        dependencies=dependencies,
        config=config,
        system_limits=_test_system_limits(),
    )
    execution_service._working_directory = tmp_path

    mock_event_store.get_events.return_value = [
        AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=None,
            config=_test_config(),
        ),
        TaskAssigned(
            aggregate_id=agent_id,
            sequence_number=2,
            task_description="Write unit tests",
        ),
        ComplexityEvaluated(
            aggregate_id=agent_id,
            sequence_number=3,
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
        ),
    ]

    await execution_service.run_agent_step(agent_id)

    assert worker_port.called is True
    assert worker_port.last_task_context is not None
    assert worker_port.last_task_context["container_session"]["container_id"] == "abc123"
    domain_plugin.prepare_worker_execution.assert_awaited_once()
    domain_plugin.cleanup_worker_execution.assert_awaited_once()


def _boss_service_with_prepared(
    tmp_path: Path, boss: AgentSession, prepared: PreparedRunWorkspace
) -> AgentExecutionService:
    """A minimally-stubbed service whose boss creation returns ``boss``/``prepared``."""
    service = object.__new__(AgentExecutionService)
    service._reset_for_new_run = MagicMock()
    service._prompt_builder = MagicMock()
    service._setup_working_directory = AsyncMock(return_value=prepared)
    service._create_shared_context = AsyncMock()
    service._create_boss_session = MagicMock(return_value=boss)
    service._get_run_metadata = MagicMock(return_value={})
    service._repository = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_create_boss_agent_emits_runtime_surface_sealed(tmp_path: Path) -> None:
    """When the domain plugin seals the runtime surface, the boss records the event."""
    # Given: a prepared workspace that reports a sealed runtime surface.
    sealed = SealedRuntimeSurface(
        surface="secbench",
        artifacts=(
            SealedRuntimeArtifact(
                container_path="/testcase/repro.sh", kind="repro_skeleton", content_sha256="a" * 64
            ),
            SealedRuntimeArtifact(
                container_path="/usr/local/bin/secb", kind="secb_wrapper", content_sha256="b" * 64
            ),
        ),
    )
    boss = AgentSession.create(
        agent_id=uuid4(), role=AgentRole.BOSS, config=_test_config(), parent_id=None
    )
    service = _boss_service_with_prepared(
        tmp_path,
        boss,
        PreparedRunWorkspace(working_directory=str(tmp_path), sealed_surface=sealed),
    )

    # When:
    await service.create_boss_agent("repro the CVE", domain_context=object())

    # Then: the saved boss aggregate emitted exactly one RuntimeSurfaceSealed event.
    saved = service._repository.save_new_agent.await_args.args[0]
    sealed_events = [e for e in saved.events if isinstance(e, RuntimeSurfaceSealed)]
    assert len(sealed_events) == 1
    assert sealed_events[0].surface == "secbench"
    assert {a.kind for a in sealed_events[0].sealed_artifacts} == {"repro_skeleton", "secb_wrapper"}


@pytest.mark.asyncio
async def test_create_boss_agent_no_seal_event_without_sealed_surface(tmp_path: Path) -> None:
    """A run with no sealed surface (generic / no plugin) emits no RuntimeSurfaceSealed."""
    # Given: a prepared workspace without a sealed surface.
    boss = AgentSession.create(
        agent_id=uuid4(), role=AgentRole.BOSS, config=_test_config(), parent_id=None
    )
    service = _boss_service_with_prepared(
        tmp_path, boss, PreparedRunWorkspace(working_directory=str(tmp_path))
    )

    # When:
    await service.create_boss_agent("generic task", domain_context=None)

    # Then: no RuntimeSurfaceSealed event is recorded.
    saved = service._repository.save_new_agent.await_args.args[0]
    assert not [e for e in saved.events if isinstance(e, RuntimeSurfaceSealed)]
