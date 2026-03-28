"""Tests for bounded system-loop termination."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bootstrap.application import ExecutionLimitsBridge
from bootstrap.tests.test_characterization import (
    FakeLLM,
    FakeSharedContextPort,
    FakeSiblingViewPort,
    FakeWorkerTool,
    InMemoryEventStore,
    _find_child_ids,
    _get_agent_status,
)
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
from core.domain.events.events import CodeGenerationStarted, DomainEvent, RunCompleted


class HangingWorker:
    """Worker that never finishes unless its task is cancelled."""

    def __init__(self) -> None:
        self.cancelled = False

    async def run_session(
        self,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=0,
            tool_name="fake",
        )

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _wire_service(
    event_store: InMemoryEventStore,
    llm: FakeLLM,
    worker: Any,
    max_run_duration_seconds: float = 0.1,
) -> AgentExecutionService:
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.01,
        output_directory="",
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )
    system_limits = ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
        max_run_duration_seconds=max_run_duration_seconds,
    )

    prompt_builder = PromptBuilder("prompts", "claude_code")
    limits_registry = HierarchyLimitsRegistry()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=config.manager_config,
    )
    orchestrator = AgentOrchestrator(
        llm_port=llm,
        worker_port=worker,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
    )
    parent_notifier = ParentNotificationService(repository=repository)

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=FakeSharedContextPort(),
        sibling_view_port=FakeSiblingViewPort(),
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
    )

    return AgentExecutionService(
        event_store=event_store,
        dependencies=dependencies,
        config=config,
        system_limits=system_limits,
    )


@pytest.mark.asyncio
async def test_run_system_loop_times_out_and_fails_active_agents() -> None:
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker = HangingWorker()
    service = _wire_service(event_store, llm, worker)

    boss_id = await service.create_boss_agent("Timeout test")
    await service.run_agent_step(boss_id)
    child_id = _find_child_ids(event_store, boss_id)[0]
    await service.run_agent_step(child_id)

    await service.run_system_loop(boss_id)

    run_completed_events = [
        event for event in event_store.events_for(boss_id)
        if isinstance(event, RunCompleted)
    ]

    assert _get_agent_status(event_store, child_id) == "failed"
    assert _get_agent_status(event_store, boss_id) == "failed"
    assert run_completed_events[-1].status == "timed_out"


@pytest.mark.asyncio
async def test_run_system_loop_completes_naturally_when_timeout_is_large() -> None:
    """When timeout is high, the loop exits on natural completion, not timeout."""
    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker = FakeWorkerTool()
    service = _wire_service(
        event_store, llm, worker, max_run_duration_seconds=9999,
    )

    boss_id = await service.create_boss_agent("Normal task")
    await service.run_system_loop(boss_id)

    run_completed_events = [
        event for event in event_store.events_for(boss_id)
        if isinstance(event, RunCompleted)
    ]

    assert run_completed_events[-1].status in ("completed", "failed")
    assert run_completed_events[-1].status != "timed_out"
