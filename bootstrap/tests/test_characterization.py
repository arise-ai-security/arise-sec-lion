"""Characterization tests for critical end-to-end flows (D12).

These integration-level tests exercise the full orchestration chain through
an in-memory event store with fake LLM/worker ports. They are the safety net
for Phase 0 structural refactors — unit tests alone cannot catch breakage in
the wiring between services.

6 critical flows:
1. Worker-only run: BOSS → single WORKER → COMPLETED
2. Multi-level run: BOSS → MANAGER → WORKERs → all COMPLETED
3. Child failure propagation: WORKER fails → parent FAILED → BOSS FAILED
4. Prompt trace replay: Events in store → trace returns expected structure
5. Cost summary: Events in store → cost projection returns expected totals
6. SSE/thought streaming: ThoughtCaptured events appear in event store
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

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
    PromptTraceService,
)
from core.domain.aggregates.agent_session import AgentRole, AgentStatus
from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    PromptSent,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from core.domain.shared_context import SharedStore
from core.domain.values.llm_response import LLMResponse, LLMUsage
from core.domain.values.node_message import Handoff
from core.domain.values.subtask import Subtask
from core.query.projections.impl import SummaryProjection


# =============================================================================
# Test Doubles
# =============================================================================


class InMemoryEventStore:
    """In-memory event store with full interface for integration tests.

    Supports all operations used by the execution service, repository,
    query service, and prompt trace service.
    """

    def __init__(self) -> None:
        self._events: dict[UUID, list[DomainEvent]] = {}

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def initialize_schema(self) -> None:
        pass

    async def append(self, event: DomainEvent) -> None:
        agent_id = event.aggregate_id
        current = self._events.get(agent_id, [])
        existing_seqs = {e.sequence_number for e in current}
        if event.sequence_number in existing_seqs:
            from core.domain.exceptions import ConcurrencyError

            raise ConcurrencyError(
                aggregate_id=str(agent_id),
                actual_version=len(current),
            )
        if agent_id not in self._events:
            self._events[agent_id] = []
        self._events[agent_id].append(event)

    async def append_batch(self, events: list[DomainEvent]) -> None:
        if not events:
            return
        agent_id = events[0].aggregate_id
        current = self._events.get(agent_id, [])
        existing_seqs = {e.sequence_number for e in current}
        if any(e.sequence_number in existing_seqs for e in events):
            from core.domain.exceptions import ConcurrencyError

            raise ConcurrencyError(
                aggregate_id=str(agent_id),
                actual_version=len(current),
            )
        if agent_id not in self._events:
            self._events[agent_id] = []
        self._events[agent_id].extend(events)

    async def get_events(
        self,
        aggregate_id: UUID,
        *,
        limit: int | None = None,
        after_sequence: int | None = None,
    ) -> list[DomainEvent]:
        events = self._events.get(aggregate_id, [])
        if after_sequence is not None:
            events = [e for e in events if e.sequence_number > after_sequence]
        if limit is not None:
            events = events[:limit]
        return events

    async def get_all_aggregate_ids(self) -> list[UUID]:
        return list(self._events.keys())

    async def count_events(self, aggregate_ids: list[UUID]) -> dict[UUID, int]:
        return {
            aggregate_id: len(self._events.get(aggregate_id, []))
            for aggregate_id in aggregate_ids
        }

    async def get_all_events_grouped(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[UUID, list[DomainEvent]]:
        items = list(self._events.items())
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return dict(items)

    async def get_hierarchy_events_grouped(
        self, root_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        """Recursive traversal via ChildSpawned events."""
        result: dict[UUID, list[DomainEvent]] = {}
        to_visit = [root_id]
        visited: set[UUID] = set()

        while to_visit:
            agent_id = to_visit.pop(0)
            if agent_id in visited:
                continue
            visited.add(agent_id)

            events = self._events.get(agent_id, [])
            if events:
                result[agent_id] = events

            for event in events:
                if isinstance(event, ChildSpawned) and event.child_id not in visited:
                    to_visit.append(event.child_id)

        return result

    async def get_children_events_grouped(
        self, parent_id: UUID
    ) -> dict[UUID, list[DomainEvent]]:
        """Get events for direct children of parent."""
        result: dict[UUID, list[DomainEvent]] = {}
        for agent_id, events in self._events.items():
            if events and isinstance(events[0], AgentCreated):
                if events[0].parent_id == parent_id:
                    result[agent_id] = events
        return result

    async def get_boss_agents_grouped(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[UUID, list[DomainEvent]]:
        result: dict[UUID, list[DomainEvent]] = {}
        for agent_id, events in self._events.items():
            if events and isinstance(events[0], AgentCreated):
                if events[0].role == "BOSS":
                    result[agent_id] = events
        return result

    async def get_boss_agent_summaries(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        return []

    async def get_events_batch_incremental(
        self, agent_sequences: dict[UUID, int | None]
    ) -> dict[UUID, list[DomainEvent]]:
        result: dict[UUID, list[DomainEvent]] = {}
        for agent_id, after_seq in agent_sequences.items():
            events = self._events.get(agent_id, [])
            if after_seq is not None:
                events = [e for e in events if e.sequence_number > after_seq]
            if events:
                result[agent_id] = events
        return result

    # --- Helpers for assertions ---

    def all_events_flat(self) -> list[DomainEvent]:
        """All events across all aggregates, for assertion convenience."""
        return [e for events in self._events.values() for e in events]

    def events_for(self, agent_id: UUID) -> list[DomainEvent]:
        return self._events.get(agent_id, [])

    def events_of_type(self, event_type: type) -> list[DomainEvent]:
        return [e for e in self.all_events_flat() if isinstance(e, event_type)]


class FakeLLM:
    """Fake LLM that returns deterministic responses based on prompt content.

    Configured with explicit response mappings for each operation type.
    """

    # Valid agent config for subtask responses
    VALID_CONFIG = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._complexity_response: str = "SIMPLE"
        self._subtask_responses: dict[str, str] = {}
        self._default_subtasks: str = json.dumps(
            [{"description": "Subtask 1: Do the thing", "config": self.VALID_CONFIG}]
        )

    def set_complexity_response(self, response: str) -> None:
        """Set response for task assessment prompts.

        Accepts "SIMPLE" or "COMPLEX". SIMPLE → {"action": "execute"},
        COMPLEX uses subtasks from set_subtasks_response.
        """
        self._complexity_response = response

    def set_subtasks_response(self, descriptions: list[str]) -> None:
        """Set subtasks for assessment decompose responses and direct decomposition.

        Args:
            descriptions: List of subtask description strings.
                          Config is auto-added.
        """
        self._default_subtasks = json.dumps(
            [{"description": d, "config": self.VALID_CONFIG} for d in descriptions]
        )

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        resp = await self.query_with_usage(prompt, config_dict)
        return resp.content

    async def query_with_usage(
        self, prompt: str, config_dict: dict[str, Any]
    ) -> LLMResponse:
        self.calls.append((prompt, config_dict))

        # Determine response based on prompt content
        content = self._determine_response(prompt)

        return LLMResponse(
            content=content,
            usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            model=config_dict.get("model", "gpt-4o"),
            cost_usd=0.01,
        )

    def _determine_response(self, prompt: str) -> str:
        # Assessment prompts (PENDING agents) contain markers from assess.j2
        if "Assess this task" in prompt:
            complexity = self._complexity_response.lower()
            if complexity == "simple":
                return json.dumps({
                    "action": "execute",
                    "reasoning": f"Task classified as {complexity}",
                })
            # Complex: return decompose with inline subtasks
            subtasks = json.loads(self._default_subtasks)
            return json.dumps({
                "action": "decompose",
                "reasoning": f"Task classified as {complexity}",
                "subtasks": subtasks,
            })
        return self._default_subtasks


class FakeWorkerTool:
    """Fake worker tool that yields deterministic events."""

    def __init__(self) -> None:
        self.executions: list[dict] = []
        self._should_fail = False
        self._failure_reason = "Worker execution failed"
        self._result = "Task completed successfully"
        self._thoughts = ["Working on it...", "Almost done..."]

    def set_failure(self, reason: str = "Worker execution failed") -> None:
        self._should_fail = True
        self._failure_reason = reason

    def set_result(self, result: str) -> None:
        self._result = result

    async def run_session(
        self, task_context: dict[str, Any]
    ) -> AsyncIterator[DomainEvent]:
        self.executions.append(task_context)
        agent_id = task_context.get("agent_id", UUID(int=0))

        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=0,
            tool_name="fake-tool",
        )

        for thought in self._thoughts:
            yield ThoughtCaptured(
                aggregate_id=agent_id,
                sequence_number=0,
                content=thought,
                stream="stdout",
            )

        if self._should_fail:
            yield WorkFailed(
                aggregate_id=agent_id,
                sequence_number=0,
                reason=self._failure_reason,
            )
        else:
            yield WorkCompleted(
                aggregate_id=agent_id,
                sequence_number=0,
                result=self._result,
            )


class FakeSharedContextPort:
    """Fake shared context port using in-memory storage."""

    def __init__(self) -> None:
        self._contexts: dict[UUID, SharedStore] = {}

    async def get_or_create(
        self,
        root_id: UUID,
        config: dict | None = None,
    ) -> SharedStore:
        if root_id not in self._contexts:
            ctx = SharedStore.create(root_id=root_id, config=config or {})
            self._contexts[root_id] = ctx
        return self._contexts[root_id]

    async def get(self, root_id: UUID) -> SharedStore | None:
        return self._contexts.get(root_id)

    async def save(
        self, context: SharedStore, expected_version: int
    ) -> None:
        self._contexts[context.root_id] = context
        context.mark_changes_as_committed()

    async def exists(self, root_id: UUID) -> bool:
        return root_id in self._contexts


class FakeSiblingViewPort:
    """Fake sibling view port returning empty handoffs."""

    async def build_view(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
        root_id: UUID,
    ) -> Handoff:
        return Handoff(
            parent_task=None,
            siblings=(),
            shared_decisions=(),
        )


class FakeRealtimeCallback:
    """Fake realtime callback that records events."""

    def __init__(self) -> None:
        self.events: list[tuple[DomainEvent, UUID]] = []

    async def on_event(self, event: DomainEvent, root_id: UUID) -> None:
        self.events.append((event, root_id))


# =============================================================================
# Fixtures
# =============================================================================


def _make_system_limits() -> ExecutionLimitsBridge:
    return ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
    )


def _make_service_config() -> ServiceConfig:
    return ServiceConfig(
        max_retries=3,
        poll_interval=0.1,
        output_directory="",
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
    )


def _wire_execution_service(
    event_store: InMemoryEventStore,
    llm: FakeLLM,
    worker: FakeWorkerTool,
    shared_context: FakeSharedContextPort | None = None,
    sibling_view: FakeSiblingViewPort | None = None,
    realtime_callback: FakeRealtimeCallback | None = None,
) -> AgentExecutionService:
    """Wire up a fully functional ExecutionService with fakes."""
    config = _make_service_config()
    system_limits = _make_system_limits()

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
        realtime_callback=realtime_callback,
    )

    parent_notifier = ParentNotificationService(repository=repository)

    dependencies = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=shared_context or FakeSharedContextPort(),
        sibling_view_port=sibling_view or FakeSiblingViewPort(),
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
    )

    return AgentExecutionService(
        event_store=event_store,
        dependencies=dependencies,
        config=config,
        system_limits=system_limits,
    )


def _subtasks_json(descriptions: list[str]) -> str:
    """Build valid subtask JSON for FakeLLM responses."""
    config = FakeLLM.VALID_CONFIG
    return json.dumps([{"description": d, "config": config} for d in descriptions])


def _assessment_execute_json() -> str:
    """Build valid assessment execute JSON for FakeLLM responses."""
    return json.dumps({
        "action": "execute",
        "reasoning": "Task classified as simple",
    })


def _assessment_decompose_json(descriptions: list[str]) -> str:
    """Build valid assessment decompose JSON with inline subtasks."""
    config = FakeLLM.VALID_CONFIG
    return json.dumps({
        "action": "decompose",
        "reasoning": "Task classified as complex",
        "subtasks": [{"description": d, "config": config} for d in descriptions],
    })


def _complexity_json(complexity: str) -> str:
    """Build valid complexity JSON for FakeLLM responses.

    DEPRECATED: Only kept for backward compatibility with smart_response functions.
    Use _assessment_execute_json() or _assessment_decompose_json() instead.
    """
    return json.dumps({
        "complexity": complexity.lower(),
        "reasoning": f"Task classified as {complexity.lower()}",
    })


def _find_child_ids(event_store: InMemoryEventStore, parent_id: UUID) -> list[UUID]:
    """Find child agent IDs by scanning ChildSpawned events."""
    child_ids = []
    for event in event_store.events_for(parent_id):
        if isinstance(event, ChildSpawned):
            child_ids.append(event.child_id)
    return child_ids


def _get_agent_status(event_store: InMemoryEventStore, agent_id: UUID) -> str:
    """Reconstruct agent status from events."""
    from core.domain.aggregates.agent_session import AgentSession

    events = event_store.events_for(agent_id)
    if not events:
        return "not_found"
    agent = AgentSession.load_from_history(events)
    return agent.status.value


def _get_agent_role(event_store: InMemoryEventStore, agent_id: UUID) -> str:
    """Reconstruct agent role from events."""
    from core.domain.aggregates.agent_session import AgentSession

    events = event_store.events_for(agent_id)
    if not events:
        return "not_found"
    agent = AgentSession.load_from_history(events)
    return agent.role.value


# =============================================================================
# Flow 1: Worker-only run (BOSS → single WORKER → COMPLETED)
# =============================================================================


class TestWorkerOnlyRun:
    """BOSS decomposes into one subtask → child evaluates as SIMPLE → WORKER completes."""

    @pytest.mark.asyncio
    async def test_boss_creates_single_child_that_completes_as_worker(self) -> None:
        # Arrange
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()

        # LLM returns SIMPLE for complexity, 1 subtask for decomposition
        llm.set_complexity_response("SIMPLE")
        llm.set_subtasks_response(["Implement the feature"])

        svc = _wire_execution_service(event_store, llm, worker)

        # Act: Create boss agent
        boss_id = await svc.create_boss_agent("Build a hello world app")

        # Step 1: Boss decomposes task → spawns 1 child
        await svc.run_agent_step(boss_id)

        # Verify boss is now WAITING with 1 child
        child_ids = _find_child_ids(event_store, boss_id)
        assert len(child_ids) == 1, "Boss should spawn exactly 1 child"
        assert _get_agent_status(event_store, boss_id) == "waiting"

        child_id = child_ids[0]

        # Step 2: Child evaluates complexity → becomes WORKER
        await svc.run_agent_step(child_id)
        assert _get_agent_role(event_store, child_id) == "worker"

        # Step 3: Worker executes task → COMPLETED
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "completed"

        # Step 4: Parent notification bubbles up → Boss COMPLETED
        assert _get_agent_status(event_store, boss_id) == "completed"

    @pytest.mark.asyncio
    async def test_events_are_persisted_through_event_store(self) -> None:
        """Verify events can be replayed to reconstruct agent state."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()
        worker.set_result("Feature implemented successfully")

        svc = _wire_execution_service(event_store, llm, worker)
        boss_id = await svc.create_boss_agent("Build feature")

        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)
        await svc.run_agent_step(child_id)

        # Replay: Load agent from stored events
        from core.domain.aggregates.agent_session import AgentSession

        boss_events = event_store.events_for(boss_id)
        boss = AgentSession.load_from_history(boss_events)
        assert boss.status == AgentStatus.COMPLETED
        assert boss.role == AgentRole.BOSS

        child_events = event_store.events_for(child_id)
        child = AgentSession.load_from_history(child_events)
        assert child.status == AgentStatus.COMPLETED
        assert child.role == AgentRole.WORKER
        assert child.result == "Feature implemented successfully"


# =============================================================================
# Flow 2: Multi-level run (BOSS → MANAGER → WORKERs → COMPLETED)
# =============================================================================


class TestMultiLevelRun:
    """BOSS → MANAGER → 2 WORKERs, all complete successfully."""

    @pytest.mark.asyncio
    async def test_three_level_hierarchy_completes(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()

        assess_count = 0

        # We need the LLM to respond differently at different stages:
        # 1st decomposition (Boss): returns 1 subtask
        # 1st assessment (child): COMPLEX → becomes MANAGER + spawns 2 children
        # 2nd+3rd assessment (grandchildren): SIMPLE → become WORKERs
        def smart_response(prompt: str) -> str:
            nonlocal assess_count

            if "Assess this task" in prompt:
                assess_count += 1
                if assess_count <= 1:
                    # 1st assessment → COMPLEX with inline subtasks
                    return _assessment_decompose_json(["Worker task A", "Worker task B"])
                # 2nd, 3rd → SIMPLE
                return _assessment_execute_json()

            # Decomposition (Boss): returns 1 subtask
            return _subtasks_json(["Handle the complex part"])

        llm._determine_response = smart_response

        svc = _wire_execution_service(event_store, llm, worker)

        # Act
        boss_id = await svc.create_boss_agent("Build a complex app")

        # Boss decomposes → 1 child
        await svc.run_agent_step(boss_id)
        manager_ids = _find_child_ids(event_store, boss_id)
        assert len(manager_ids) == 1
        manager_id = manager_ids[0]

        # Child assesses → COMPLEX → becomes MANAGER + spawns 2 children in one step
        await svc.run_agent_step(manager_id)
        assert _get_agent_role(event_store, manager_id) == "manager"
        worker_ids = _find_child_ids(event_store, manager_id)
        assert len(worker_ids) == 2
        assert _get_agent_status(event_store, manager_id) == "waiting"

        # Workers assess → SIMPLE → become WORKERs
        for wid in worker_ids:
            await svc.run_agent_step(wid)
            assert _get_agent_role(event_store, wid) == "worker"

        # Workers execute → COMPLETED
        for wid in worker_ids:
            await svc.run_agent_step(wid)
            assert _get_agent_status(event_store, wid) == "completed"

        # Parent notifications bubble up:
        # - Both workers complete → manager COMPLETED
        # - Manager complete → boss COMPLETED
        assert _get_agent_status(event_store, manager_id) == "completed"
        assert _get_agent_status(event_store, boss_id) == "completed"

    @pytest.mark.asyncio
    async def test_hierarchy_events_grouped_returns_full_tree(self) -> None:
        """Verify get_hierarchy_events_grouped finds all descendants."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()

        assess_count = 0

        def smart_response(prompt: str) -> str:
            nonlocal assess_count
            if "Assess this task" in prompt:
                assess_count += 1
                if assess_count <= 1:
                    return _assessment_decompose_json(["A", "B"])
                return _assessment_execute_json()
            return _subtasks_json(["Sub"])

        llm._determine_response = smart_response
        svc = _wire_execution_service(event_store, llm, worker)

        boss_id = await svc.create_boss_agent("Task")
        await svc.run_agent_step(boss_id)
        manager_id = _find_child_ids(event_store, boss_id)[0]
        # assess_task: COMPLEX → MANAGER + spawns 2 children in one step
        await svc.run_agent_step(manager_id)
        worker_ids = _find_child_ids(event_store, manager_id)

        # Verify hierarchy traversal finds all 4 agents
        grouped = await event_store.get_hierarchy_events_grouped(boss_id)
        assert boss_id in grouped
        assert manager_id in grouped
        for wid in worker_ids:
            assert wid in grouped
        assert len(grouped) == 4  # boss + manager + 2 workers


# =============================================================================
# Flow 3: Child failure propagation
# =============================================================================


class TestChildFailurePropagation:
    """WORKER fails → parent FAILED → BOSS FAILED."""

    @pytest.mark.asyncio
    async def test_worker_failure_propagates_to_boss(self) -> None:
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()
        worker.set_failure("Out of memory")

        svc = _wire_execution_service(event_store, llm, worker)

        # Boss decomposes → 1 child
        boss_id = await svc.create_boss_agent("Failing task")
        await svc.run_agent_step(boss_id)

        child_id = _find_child_ids(event_store, boss_id)[0]

        # Child evaluates → WORKER
        await svc.run_agent_step(child_id)
        assert _get_agent_role(event_store, child_id) == "worker"

        # Worker fails
        await svc.run_agent_step(child_id)
        assert _get_agent_status(event_store, child_id) == "failed"

        # Failure propagates to boss
        assert _get_agent_status(event_store, boss_id) == "failed"

    @pytest.mark.asyncio
    async def test_failure_propagates_through_manager(self) -> None:
        """WORKER fails → MANAGER FAILED → BOSS FAILED (3-level propagation)."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()
        worker.set_failure("Timeout")

        assess_count = 0

        def smart_response(prompt: str) -> str:
            nonlocal assess_count
            if "Assess this task" in prompt:
                assess_count += 1
                if assess_count <= 1:
                    # 1st assessment → COMPLEX with 1 inline subtask
                    return _assessment_decompose_json(["Worker task"])
                return _assessment_execute_json()
            # Boss decomposition
            return _subtasks_json(["Sub"])

        llm._determine_response = smart_response
        svc = _wire_execution_service(event_store, llm, worker)

        boss_id = await svc.create_boss_agent("Complex failing task")
        await svc.run_agent_step(boss_id)

        manager_id = _find_child_ids(event_store, boss_id)[0]
        # assess_task: COMPLEX → MANAGER + spawns 1 worker child in one step
        await svc.run_agent_step(manager_id)

        worker_id = _find_child_ids(event_store, manager_id)[0]
        await svc.run_agent_step(worker_id)  # assess → WORKER
        await svc.run_agent_step(worker_id)  # execute → FAILED

        # Failure propagation: worker → manager → boss
        assert _get_agent_status(event_store, worker_id) == "failed"
        assert _get_agent_status(event_store, manager_id) == "failed"
        assert _get_agent_status(event_store, boss_id) == "failed"


# =============================================================================
# Flow 4: Prompt trace replay
# =============================================================================


class TestPromptTraceReplay:
    """Events in store → PromptTraceService returns expected hierarchy structure."""

    @pytest.mark.asyncio
    async def test_trace_returns_hierarchy_structure(self) -> None:
        """Seed events for BOSS → 2 children, verify trace structure."""
        event_store = InMemoryEventStore()
        boss_id = UUID("00000000-0000-0000-0000-000000000001")
        child1_id = UUID("00000000-0000-0000-0000-000000000002")
        child2_id = UUID("00000000-0000-0000-0000-000000000003")

        base_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)

        # Boss events
        for event in [
            AgentCreated(
                aggregate_id=boss_id,
                sequence_number=1,
                role="BOSS",
                parent_id=None,
                config={},
                occurred_at=base_time,
            ),
            TaskAssigned(
                aggregate_id=boss_id,
                sequence_number=2,
                task_description="Build app",
                occurred_at=base_time + timedelta(seconds=1),
            ),
            PromptSent(
                aggregate_id=boss_id,
                sequence_number=3,
                prompt="Decompose: Build app",
                prompt_type="task_decomposition",
                target="llm",
                occurred_at=base_time + timedelta(seconds=2),
            ),
            ChildSpawned(
                aggregate_id=boss_id,
                sequence_number=4,
                child_id=child1_id,
                child_role="PENDING",
                subtask=Subtask(description="Backend", config=FakeLLM.VALID_CONFIG),
                child_config=FakeLLM.VALID_CONFIG,
                occurred_at=base_time + timedelta(seconds=3),
            ),
            ChildSpawned(
                aggregate_id=boss_id,
                sequence_number=5,
                child_id=child2_id,
                child_role="PENDING",
                subtask=Subtask(description="Frontend", config=FakeLLM.VALID_CONFIG),
                child_config=FakeLLM.VALID_CONFIG,
                sibling_index=1,
                occurred_at=base_time + timedelta(seconds=3),
            ),
        ]:
            await event_store.append(event)

        # Child 1 events
        for event in [
            AgentCreated(
                aggregate_id=child1_id,
                sequence_number=1,
                role="PENDING",
                parent_id=boss_id,
                config={},
                occurred_at=base_time + timedelta(seconds=4),
            ),
            TaskAssigned(
                aggregate_id=child1_id,
                sequence_number=2,
                task_description="Backend",
                occurred_at=base_time + timedelta(seconds=5),
            ),
            ComplexityEvaluated(
                aggregate_id=child1_id,
                sequence_number=3,
                complexity="simple",
                determined_role="WORKER",
                occurred_at=base_time + timedelta(seconds=6),
            ),
        ]:
            await event_store.append(event)

        # Child 2 events
        for event in [
            AgentCreated(
                aggregate_id=child2_id,
                sequence_number=1,
                role="PENDING",
                parent_id=boss_id,
                config={},
                sibling_index=1,
                occurred_at=base_time + timedelta(seconds=4),
            ),
            TaskAssigned(
                aggregate_id=child2_id,
                sequence_number=2,
                task_description="Frontend",
                occurred_at=base_time + timedelta(seconds=5),
            ),
        ]:
            await event_store.append(event)

        # Act
        trace_service = PromptTraceService(event_store=event_store)
        trace = await trace_service.trace(boss_id)

        # Assert
        assert trace.total_agents == 3
        assert trace.max_depth == 1  # 0-indexed: boss=0, children=1
        assert trace.root.agent_id == boss_id
        assert trace.root.role.upper() == "BOSS"
        assert trace.root.task == "Build app"
        assert len(trace.root.children) == 2

        # Children sorted by sibling_index
        assert trace.root.children[0].agent_id == child1_id
        assert trace.root.children[0].role.upper() == "WORKER"
        assert trace.root.children[0].task == "Backend"

        assert trace.root.children[1].agent_id == child2_id
        assert trace.root.children[1].role.upper() == "PENDING"
        assert trace.root.children[1].task == "Frontend"

    @pytest.mark.asyncio
    async def test_trace_after_full_execution(self) -> None:
        """Run a complete flow, then verify trace from stored events."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()

        svc = _wire_execution_service(event_store, llm, worker)
        boss_id = await svc.create_boss_agent("Trace test task")

        # Execute full flow
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)
        await svc.run_agent_step(child_id)

        # Verify trace
        trace_service = PromptTraceService(event_store=event_store)
        trace = await trace_service.trace(boss_id)

        assert trace.total_agents == 2  # boss + 1 worker
        assert trace.root.agent_id == boss_id
        assert trace.root.role.upper() == "BOSS"
        assert len(trace.root.children) == 1
        assert trace.root.children[0].role.upper() == "WORKER"


# =============================================================================
# Flow 5: Cost summary
# =============================================================================


class TestCostSummary:
    """Events in store → SummaryProjection returns expected cost totals."""

    def test_cost_projection_from_seeded_events(self) -> None:
        """Seed events with token/cost data, verify projection totals."""
        boss_id = UUID("00000000-0000-0000-0000-000000000001")
        worker_id = UUID("00000000-0000-0000-0000-000000000002")
        base_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)

        events = [
            AgentCreated(
                aggregate_id=boss_id,
                sequence_number=1,
                role="BOSS",
                parent_id=None,
                config={},
                occurred_at=base_time,
            ),
            TokensConsumed(
                aggregate_id=boss_id,
                sequence_number=2,
                model="gpt-4o",
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost_usd=0.10,
                operation="task_decomposition",
                occurred_at=base_time + timedelta(seconds=1),
            ),
            AgentCreated(
                aggregate_id=worker_id,
                sequence_number=1,
                role="WORKER",
                parent_id=boss_id,
                config={},
                occurred_at=base_time + timedelta(seconds=2),
            ),
            TokensConsumed(
                aggregate_id=worker_id,
                sequence_number=2,
                model="gpt-4o-mini",
                prompt_tokens=200,
                completion_tokens=100,
                total_tokens=300,
                cost_usd=0.01,
                operation="complexity_evaluation",
                occurred_at=base_time + timedelta(seconds=3),
            ),
            WorkerCostRecorded(
                aggregate_id=worker_id,
                sequence_number=3,
                tool_name="claude_code",
                model="claude-sonnet-4-20250514",
                tokens=5000,
                cost_usd=0.50,
                duration_seconds=120.0,
                occurred_at=base_time + timedelta(seconds=4),
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        # Total costs
        assert result.cost is not None
        assert result.cost.llm_cost_usd == pytest.approx(0.11)  # 0.10 + 0.01
        assert result.cost.worker_cost_usd == pytest.approx(0.50)
        assert result.cost.total_cost_usd == pytest.approx(0.61)

        # By role
        assert result.cost.cost_by_role["BOSS"] == pytest.approx(0.10)
        assert result.cost.cost_by_role["WORKER"] == pytest.approx(0.51)  # 0.01 + 0.50

        # By model
        assert result.cost.cost_by_model["gpt-4o"] == pytest.approx(0.10)
        assert result.cost.cost_by_model["gpt-4o-mini"] == pytest.approx(0.01)

    def test_cost_projection_after_execution(self) -> None:
        """Run execution, collect all events, verify cost projection."""
        import asyncio

        async def _run() -> None:
            event_store = InMemoryEventStore()
            llm = FakeLLM()
            worker = FakeWorkerTool()

            svc = _wire_execution_service(event_store, llm, worker)
            boss_id = await svc.create_boss_agent("Cost test")

            await svc.run_agent_step(boss_id)
            child_id = _find_child_ids(event_store, boss_id)[0]
            await svc.run_agent_step(child_id)
            await svc.run_agent_step(child_id)

            # Project all events
            all_events = event_store.all_events_flat()
            projection = SummaryProjection()
            result = projection.project(all_events)

            # Should have cost data from LLM calls
            assert result.cost is not None
            token_events = event_store.events_of_type(TokensConsumed)
            assert len(token_events) >= 1, "Should have TokensConsumed events from LLM calls"
            assert result.cost.llm_cost_usd > 0

        asyncio.run(_run())


# =============================================================================
# Flow 6: SSE / Thought streaming
# =============================================================================


class TestThoughtStreaming:
    """ThoughtCaptured events from worker execution appear in event store."""

    @pytest.mark.asyncio
    async def test_thought_events_persisted_in_store(self) -> None:
        """Worker execution produces ThoughtCaptured events in the event store."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()
        worker._thoughts = ["Analyzing requirements...", "Writing code...", "Testing..."]

        svc = _wire_execution_service(event_store, llm, worker)
        boss_id = await svc.create_boss_agent("Thought test")

        # Boss → 1 child
        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]

        # Child → WORKER
        await svc.run_agent_step(child_id)

        # Worker executes → ThoughtCaptured events emitted
        await svc.run_agent_step(child_id)

        # Verify ThoughtCaptured events in worker's event stream
        worker_events = event_store.events_for(child_id)
        thought_events = [e for e in worker_events if isinstance(e, ThoughtCaptured)]
        assert len(thought_events) == 3
        assert thought_events[0].content == "Analyzing requirements..."
        assert thought_events[1].content == "Writing code..."
        assert thought_events[2].content == "Testing..."

    @pytest.mark.asyncio
    async def test_realtime_callback_receives_worker_events(self) -> None:
        """Realtime callback receives events during worker execution."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()
        realtime = FakeRealtimeCallback()

        svc = _wire_execution_service(
            event_store, llm, worker, realtime_callback=realtime
        )
        boss_id = await svc.create_boss_agent("Realtime test")

        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)
        await svc.run_agent_step(child_id)

        # Verify realtime callback received ThoughtCaptured events
        thought_events = [
            (e, rid) for e, rid in realtime.events if isinstance(e, ThoughtCaptured)
        ]
        assert len(thought_events) >= 1, "Realtime callback should receive ThoughtCaptured events"

    @pytest.mark.asyncio
    async def test_hierarchy_events_include_thoughts(self) -> None:
        """get_hierarchy_events_grouped includes ThoughtCaptured events."""
        event_store = InMemoryEventStore()
        llm = FakeLLM()
        worker = FakeWorkerTool()

        svc = _wire_execution_service(event_store, llm, worker)
        boss_id = await svc.create_boss_agent("Hierarchy thought test")

        await svc.run_agent_step(boss_id)
        child_id = _find_child_ids(event_store, boss_id)[0]
        await svc.run_agent_step(child_id)
        await svc.run_agent_step(child_id)

        # Verify hierarchy query includes thought events
        grouped = await event_store.get_hierarchy_events_grouped(boss_id)
        child_events = grouped[child_id]
        thought_events = [e for e in child_events if isinstance(e, ThoughtCaptured)]
        assert len(thought_events) >= 1
