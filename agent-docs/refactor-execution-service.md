# Refactor execution_service.py for Software Engineering Quality

## Problem Summary

`core/application/execution_service.py` (482 lines) is a "God Class" violating Single Responsibility Principle with 10+ concerns in one class.

**Critical Issues:**
- `run_agent_step()`: 76 lines, 12 responsibilities
- Agent reconstruction pattern duplicated 8+ times
- Event persistence pattern duplicated 5+ times
- Mixed abstractions (file I/O with domain logic)
- Tight coupling, hard to test

## Proposed Architecture

Extract 6 focused services, each with single responsibility:

```
core/application/
├── execution_service.py          # Simplified facade (~150 lines)
└── services/
    ├── __init__.py
    ├── agent_repository.py       # Load/save agents with OCC
    ├── budget_tracker.py         # Cost tracking & enforcement
    ├── child_lifecycle.py        # Child spawning & parent notification
    ├── workspace_manager.py      # Working directory & context
    ├── statistics_service.py     # Query operations
    └── occ_retry.py              # Retry logic utility
```

## Services to Extract

### 1. `AgentRepository` (Unit of Work pattern)
**Eliminates:** 8+ duplications of agent reconstruction, 5+ duplications of event persistence

```python
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from core.domain.events import DomainEvent
from core.domain.model import AgentSession
from core.ports.event_store_port import EventStorePort


class AgentNotFoundError(Exception):
    """Agent not found in event store."""
    def __init__(self, agent_id: UUID) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent {agent_id} not found in event store")


@dataclass
class AgentUnitOfWork:
    """Tracks agent state and uncommitted events for atomic persistence."""
    agent: AgentSession
    initial_version: int

    @property
    def uncommitted_events(self) -> list[DomainEvent]:
        return self.agent.events

    def mark_committed(self) -> None:
        self.agent.mark_changes_as_committed()


class AgentRepository:
    """Load and persist agents with OCC. Encapsulates event sourcing mechanics."""

    def __init__(self, event_store: EventStorePort) -> None:
        self._event_store = event_store

    async def load(self, agent_id: UUID) -> AgentUnitOfWork:
        """Load agent from events. Raises AgentNotFoundError if not found."""
        events = await self._event_store.get_events(agent_id)
        if not events:
            raise AgentNotFoundError(agent_id)
        agent = AgentSession.load_from_history(events)
        return AgentUnitOfWork(agent=agent, initial_version=agent.version)

    async def load_if_exists(self, agent_id: UUID) -> AgentUnitOfWork | None:
        """Load agent, return None if not found."""
        events = await self._event_store.get_events(agent_id)
        if not events:
            return None
        agent = AgentSession.load_from_history(events)
        return AgentUnitOfWork(agent=agent, initial_version=agent.version)

    async def save(
        self,
        uow: AgentUnitOfWork,
        on_event_persisted: Callable[[DomainEvent, AgentSession], None] | None = None,
    ) -> None:
        """Persist uncommitted events with OCC."""
        current_version = uow.initial_version
        for event in uow.uncommitted_events:
            await self._event_store.append(event, expected_version=current_version)
            current_version += 1
            if on_event_persisted:
                on_event_persisted(event, uow.agent)
        uow.mark_committed()

    async def create_and_save(
        self,
        agent: AgentSession,
        on_event_persisted: Callable[[DomainEvent, AgentSession], None] | None = None,
    ) -> None:
        """Persist a newly created agent (version starts at 0)."""
        for idx, event in enumerate(agent.events):
            await self._event_store.append(event, expected_version=idx)
            if on_event_persisted:
                on_event_persisted(event, agent)
        agent.mark_changes_as_committed()

    async def get_all_ids(self) -> list[UUID]:
        """Get all agent IDs in the store."""
        return await self._event_store.get_all_aggregate_ids()
```

### 2. `BudgetTracker`
**Eliminates:** `_track_cost()`, `_is_budget_exceeded()`, `_handle_budget_exceeded()`

```python
from dataclasses import dataclass
from typing import Callable

from core.domain.events import BudgetExceeded, DomainEvent, TokensConsumed
from core.domain.model import AgentSession


@dataclass
class BudgetConfig:
    """Budget configuration for cost tracking and limits."""
    max_total_cost_usd: float
    cost_warning_threshold: float
    cost_tracking_enabled: bool


@dataclass
class BudgetStatus:
    """Current budget status."""
    total_cost_usd: float
    is_exceeded: bool
    remaining_usd: float
    warning_threshold_reached: bool


class BudgetTracker:
    """Track costs and enforce budget limits across all agents."""

    def __init__(
        self,
        config: BudgetConfig,
        on_warning: Callable[[float, float], None] | None = None,
    ) -> None:
        self._config = config
        self._total_cost_usd: float = 0.0
        self._warning_emitted: bool = False
        self._on_warning = on_warning

    @property
    def total_cost(self) -> float:
        return self._total_cost_usd

    @property
    def is_exceeded(self) -> bool:
        if not self._config.cost_tracking_enabled:
            return False
        return self._total_cost_usd >= self._config.max_total_cost_usd

    def get_status(self) -> BudgetStatus:
        return BudgetStatus(
            total_cost_usd=self._total_cost_usd,
            is_exceeded=self.is_exceeded,
            remaining_usd=max(0, self._config.max_total_cost_usd - self._total_cost_usd),
            warning_threshold_reached=self._warning_emitted,
        )

    def track_event(self, event: DomainEvent) -> None:
        if not self._config.cost_tracking_enabled:
            return
        if isinstance(event, TokensConsumed):
            self._total_cost_usd += event.cost_usd
            self._check_warning_threshold()

    def _check_warning_threshold(self) -> None:
        if self._warning_emitted:
            return
        threshold = self._config.max_total_cost_usd * self._config.cost_warning_threshold
        if self._total_cost_usd >= threshold:
            self._warning_emitted = True
            if self._on_warning:
                self._on_warning(self._total_cost_usd, self._config.max_total_cost_usd)

    def create_exceeded_event(self, agent: AgentSession) -> BudgetExceeded:
        return BudgetExceeded(
            aggregate_id=agent.session_id,
            sequence_number=agent._next_sequence(),
            budget_limit_usd=self._config.max_total_cost_usd,
            current_total_usd=self._total_cost_usd,
            exceeded_by_usd=self._total_cost_usd - self._config.max_total_cost_usd,
        )

    def reset(self) -> None:
        self._total_cost_usd = 0.0
        self._warning_emitted = False
```

### 3. `ChildLifecycleManager`
**Eliminates:** `_handle_child_spawning()`, `_handle_parent_notification()`, context tracking

```python
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from core.domain.events import ChildSpawned, DomainEvent
from core.domain.execution_context import ExecutionContext
from core.domain.model import AgentRole, AgentSession, AgentStatus

from .agent_repository import AgentRepository


@dataclass
class SystemLimits:
    """System-wide limits for agent creation."""
    max_total_agents: int  # -1 = unlimited

    def is_agents_limited(self) -> bool:
        return self.max_total_agents > 0


class ChildLifecycleManager:
    """Manage child agent spawning and parent notification."""

    def __init__(self, repository: AgentRepository, limits: SystemLimits) -> None:
        self._repository = repository
        self._limits = limits
        self._total_agents_created: int = 0
        self._execution_contexts: dict[UUID, ExecutionContext] = {}

    def set_execution_context(self, agent_id: UUID, context: ExecutionContext) -> None:
        self._execution_contexts[agent_id] = context

    def get_execution_context(self, agent_id: UUID) -> ExecutionContext | None:
        return self._execution_contexts.get(agent_id)

    def clear_contexts(self) -> None:
        self._execution_contexts.clear()

    def reset_agent_count(self, initial_count: int = 1) -> None:
        self._total_agents_created = initial_count

    async def spawn_children(
        self,
        parent: AgentSession,
        events: list[DomainEvent],
        on_event: Callable[[DomainEvent, AgentSession], None] | None = None,
    ) -> list[UUID]:
        """Create child agents from ChildSpawned events."""
        child_spawned = [e for e in events if isinstance(e, ChildSpawned)]
        parent_context = self._execution_contexts.get(parent.session_id)
        created_ids: list[UUID] = []

        for child_event in child_spawned:
            if (self._limits.is_agents_limited() and
                self._total_agents_created >= self._limits.max_total_agents):
                continue

            child = AgentSession.create(
                session_id=child_event.child_id,
                role=AgentRole(child_event.child_role),
                config=child_event.child_config,
                parent_id=parent.session_id,
            )
            child.assign_task(child_event.subtask.description)

            if parent_context is not None:
                self._execution_contexts[child_event.child_id] = parent_context.for_child()

            await self._repository.create_and_save(child, on_event)
            self._total_agents_created += 1
            created_ids.append(child_event.child_id)

        return created_ids

    async def notify_parent_of_completion(
        self,
        child: AgentSession,
        on_event: Callable[[DomainEvent, AgentSession], None] | None = None,
    ) -> bool:
        """Notify parent when child completes."""
        if child.status != AgentStatus.COMPLETED or child.parent_id is None:
            return False

        parent_uow = await self._repository.load(child.parent_id)
        parent_uow.agent.handle_child_update(child.session_id, child.result or "")
        await self._repository.save(parent_uow, on_event)
        return True
```

### 4. `WorkspaceManager`
**Eliminates:** `_get_workspace_context()`, directory creation from `create_boss_agent()`

```python
from pathlib import Path
from uuid import UUID


class WorkspaceManager:
    """Manage working directories and workspace context for agents."""

    def __init__(self, base_output_directory: str) -> None:
        self._base_output_directory = base_output_directory
        self._working_directory: str | None = None
        self._workspace_context_cache: str | None = None
        self._workspace_context_scanned: bool = False

    @property
    def working_directory(self) -> str | None:
        return self._working_directory

    def setup_run_directory(self, run_id: UUID) -> str | None:
        """Create working directory for a run."""
        self.reset()
        if not self._base_output_directory:
            return None

        base_output = Path(self._base_output_directory).resolve()
        base_output.mkdir(parents=True, exist_ok=True)
        run_output_path = base_output / str(run_id)
        run_output_path.mkdir(parents=True, exist_ok=True)
        self._working_directory = str(run_output_path)
        return self._working_directory

    def get_workspace_context(self) -> str | None:
        """Get file listing for workspace. Cached after first scan."""
        if self._workspace_context_scanned:
            return self._workspace_context_cache

        self._workspace_context_scanned = True
        if not self._working_directory:
            return None

        workspace_path = Path(self._working_directory)
        if not workspace_path.exists():
            return None

        try:
            files = []
            for item in workspace_path.rglob("*"):
                if any(part.startswith(".") for part in item.parts):
                    continue
                if item.is_file():
                    files.append(str(item.relative_to(workspace_path)))

            if not files:
                return None

            files.sort()
            self._workspace_context_cache = "\n".join(f"- {f}" for f in files)
            return self._workspace_context_cache
        except OSError:
            return None

    def reset(self) -> None:
        self._working_directory = None
        self._workspace_context_cache = None
        self._workspace_context_scanned = False
```

### 5. `StatisticsService`
**Eliminates:** `get_agent_result()`, `get_system_statistics()`, `_get_active_agent_ids()`

```python
from uuid import UUID

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.model import AgentStatus

from .agent_repository import AgentRepository


class StatisticsService:
    """Query service for agent statistics and results."""

    def __init__(self, repository: AgentRepository) -> None:
        self._repository = repository

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        uow = await self._repository.load(agent_id)
        agent = uow.agent
        return AgentResultDTO(
            agent_id=str(agent.session_id),
            status=agent.status.value,
            result=agent.result,
            task_description=agent.task_description or "",
            role=agent.role.value,
        )

    async def get_system_statistics(self) -> SystemStatisticsDTO:
        all_ids = await self._repository.get_all_ids()
        completed, failed, active = 0, 0, 0

        for agent_id in all_ids:
            uow = await self._repository.load_if_exists(agent_id)
            if uow is None:
                continue
            if uow.agent.status == AgentStatus.COMPLETED:
                completed += 1
            elif uow.agent.status == AgentStatus.FAILED:
                failed += 1
            else:
                active += 1

        return SystemStatisticsDTO(
            total_agents=len(all_ids),
            completed=completed,
            failed=failed,
            active=active,
        )

    async def get_active_agent_ids(self) -> list[UUID]:
        all_ids = await self._repository.get_all_ids()
        return [
            agent_id for agent_id in all_ids
            if (uow := await self._repository.load_if_exists(agent_id))
            and not uow.agent.is_terminal()
        ]
```

### 6. `OCCRetryExecutor`
**Eliminates:** Retry loop in `run_agent_step()`

```python
from typing import Awaitable, Callable, TypeVar

from core.domain.exceptions import ConcurrencyError

T = TypeVar("T")


class OCCRetryExecutor:
    """Execute operations with OCC retry logic."""

    def __init__(self, max_retries: int) -> None:
        self._max_retries = max_retries

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        on_retry: Callable[[int, ConcurrencyError], None] | None = None,
    ) -> T:
        """Execute operation with OCC retry."""
        retry_count = 0
        last_error: ConcurrencyError | None = None

        while retry_count < self._max_retries:
            try:
                return await operation()
            except ConcurrencyError as e:
                retry_count += 1
                last_error = e
                if on_retry:
                    on_retry(retry_count, e)
                if retry_count >= self._max_retries:
                    raise

        assert last_error is not None
        raise last_error
```

## Simplified `AgentExecutionService` (Facade)

After extraction, the main service becomes a thin orchestration facade (~150 lines):

```python
class AgentExecutionService:
    """Orchestrates agent workflow. Delegates to specialized services."""

    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        system_limits: SystemLimitsConfig,
        model_config: dict[str, str],
        max_retries: int,
        poll_interval: float,
        output_directory: str,
        default_worker_tool: str,
        budget_config: BudgetConfig,
        prompt_builder: PromptBuilder | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        # Ports
        self._llm_port = llm_port
        self._worker_tool_port = worker_tool_port

        # Configuration
        self._model_config = model_config
        self._poll_interval = poll_interval
        self._prompt_builder = prompt_builder or PromptBuilder(default_tool=default_worker_tool)
        self._progress_callback = progress_callback
        self._system_limits = system_limits

        # Composed services
        self._repository = AgentRepository(event_store)
        self._budget_tracker = BudgetTracker(config=budget_config, on_warning=self._emit_budget_warning)
        self._child_lifecycle = ChildLifecycleManager(
            repository=self._repository,
            limits=SystemLimits(max_total_agents=system_limits.max_total_agents),
        )
        self._workspace = WorkspaceManager(output_directory)
        self._statistics = StatisticsService(self._repository)
        self._occ_retry = OCCRetryExecutor(max_retries)

        # Worker concurrency
        semaphore_limit = system_limits.max_concurrent_workers if system_limits.is_workers_limited() else 10000
        self._worker_semaphore = asyncio.Semaphore(semaphore_limit)

    async def run_agent_step(self, agent_id: UUID) -> None:
        await self._occ_retry.execute(lambda: self._execute_step(agent_id))

    async def _execute_step(self, agent_id: UUID) -> None:
        """Single step execution (< 20 lines)."""
        uow = await self._repository.load(agent_id)

        context = self._child_lifecycle.get_execution_context(agent_id)
        if context:
            uow.agent.set_execution_context(context)

        if self._budget_tracker.is_exceeded:
            await self._fail_agent_for_budget(uow)
            return

        await self._dispatch_agent_action(uow.agent)
        uncommitted = list(uow.uncommitted_events)
        await self._repository.save(uow, self._on_event_persisted)

        if self._budget_tracker.is_exceeded and not uow.agent.is_terminal():
            uow = await self._repository.load(agent_id)
            await self._fail_agent_for_budget(uow)
            return

        await self._child_lifecycle.spawn_children(uow.agent, uncommitted, self._on_event_persisted)
        await self._child_lifecycle.notify_parent_of_completion(uow.agent, self._on_event_persisted)

    # ... remaining methods delegating to services
```

## Implementation Order

### Phase 1: Foundation (Low Risk)
1. Create `services/` directory and `__init__.py`
2. Extract `AgentRepository` (biggest impact, eliminates most duplication)
3. Extract `WorkspaceManager` (simple, no dependencies)

### Phase 2: Budget & Retry (Medium Risk)
4. Extract `BudgetTracker`
5. Extract `OCCRetryExecutor`

### Phase 3: Child Lifecycle (Medium Risk)
6. Extract `ChildLifecycleManager` (depends on AgentRepository)

### Phase 4: Query & Integration (Low Risk)
7. Extract `StatisticsService`
8. Refactor `AgentExecutionService` to use composed services
9. Update tests and bootstrap wiring

## Files to Modify

**Create:**
- `core/application/services/__init__.py`
- `core/application/services/agent_repository.py`
- `core/application/services/budget_tracker.py`
- `core/application/services/child_lifecycle.py`
- `core/application/services/workspace_manager.py`
- `core/application/services/statistics_service.py`
- `core/application/services/occ_retry.py`

**Modify:**
- `core/application/execution_service.py` - Refactor to facade
- `bootstrap/application.py` - Wire new services
- `core/application/tests/test_execution_service.py` - Update imports

## Expected Outcome

| Metric | Before | After |
|--------|--------|-------|
| Lines in main service | 482 | ~150 |
| Methods over 20 lines | 5 | 0 |
| Testable units | 1 | 6 |
| Code duplication | High | Eliminated |
| Single Responsibility | No | Yes |

## Architectural Notes

- All services stay in `core/application/` (hexagonal compliance)
- No new ports needed - services use existing `EventStorePort`
- `BudgetConfig` lives in `budget_tracker.py` for cohesion
- Existing tests should pass with minimal changes
