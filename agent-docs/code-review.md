# Arise Sec Lion - Code Review Guide

## Table of Contents
1. [High-Level Architecture](#high-level-architecture)
2. [Domain Layer](#domain-layer)
3. [Ports Layer (Abstractions)](#ports-layer)
4. [Infrastructure Layer](#infrastructure-layer)
5. [Application Layer](#application-layer)
6. [Security Benchmark Feature](#security-benchmark-feature)

---

## High-Level Architecture

### System Overview

Arise Sec Lion is a **recursive, self-healing multi-agent orchestration platform** that:
- Decomposes complex tasks into subtasks
- Executes tasks via specialized "black box" tools (Claude Code, OpenHands)
- Uses event sourcing for complete audit trails and state reconstruction

### Architectural Patterns

| Pattern | Purpose |
|---------|---------|
| **Hexagonal Architecture** | Isolates domain logic from infrastructure |
| **Event Sourcing** | State derived from replaying immutable events |
| **CQRS** | Separates command (write) and query (read) models |
| **Ports & Adapters** | Abstract interfaces with pluggable implementations |

### Dependency Rule

```
Infrastructure → Ports ← Domain Core
      ↑                      ↑
   Bootstrap ───────────────┘
```

**Critical Rule**: `core/` NEVER imports from `infrastructure/`.

### Agent Hierarchy

```
BOSS (root)              ─── Decomposes task, orchestrates
  │
  ├── PENDING            ─── Evaluates complexity
  │     ├── SIMPLE → WORKER      ─── Executes task directly
  │     └── COMPLEX → MANAGER    ─── Further decomposes
  │
  └── MANAGER            ─── Recursive decomposition
        └── PENDING → ...
```

---

## Domain Layer

**Location**: `core/domain/`

The domain layer contains pure business logic with zero infrastructure dependencies.

### 1. AgentSession (`model.py:60-534`)

The **aggregate root** - all agent state flows through this class.

```python
class AgentSession:
    """Event-sourced aggregate for agent sessions."""

    # Core Identity
    session_id: UUID
    role: AgentRole       # BOSS, PENDING, MANAGER, WORKER
    status: AgentStatus   # PENDING, ANALYZING, IN_PROGRESS, WAITING, COMPLETED, FAILED

    # Hierarchy
    parent_id: UUID | None
    child_ids: list[UUID]
    child_results: dict[UUID, str]

    # Task
    task_description: str
    result: str | None
    config: AgentConfig

    # Event Sourcing
    version: int
    _changes: list[DomainEvent]  # Uncommitted events
```

**Key Methods**:

| Method | Role | Purpose |
|--------|------|---------|
| `evaluate_complexity()` | PENDING | LLM call → determines WORKER or MANAGER |
| `evaluate_task()` | BOSS/MANAGER | LLM call → decomposes into subtasks |
| `execute_task()` | WORKER | Runs Claude Code/OpenHands |
| `handle_child_update()` | BOSS/MANAGER | Aggregates child results |

**State Reconstruction**:
```python
@classmethod
def load_from_history(cls, events: list[DomainEvent]) -> "AgentSession":
    """Reconstruct state by replaying events."""
    instance = cls(first_event.aggregate_id)
    for event in events:
        instance._apply(event)  # Mutates internal state
    return instance
```

### 2. Domain Events (`events.py`)

Immutable facts that happened in the system:

| Event | Emitter | Data |
|-------|---------|------|
| `AgentCreated` | Factory | role, parent_id, config |
| `TaskAssigned` | assign_task() | task_description |
| `ComplexityEvaluated` | evaluate_complexity() | complexity, determined_role |
| `SubtasksDefined` | evaluate_task() | list[Subtask] |
| `ChildSpawned` | evaluate_task() | child_id, child_role, subtask |
| `WorkCompleted` | execute_task() / handle_child_update() | result |
| `WorkFailed` | any | reason |
| `TokensConsumed` | LLM calls | model, tokens, cost_usd |

**Event Structure**:
```python
class DomainEvent(BaseModel):
    model_config = {"frozen": True}  # Immutable!

    event_id: UUID
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime
    metadata: dict[str, Any]
```

### 3. PromptBuilder (`prompt_builder.py`)

Composes hierarchical prompts from Jinja2 templates:

```
System Role  →  Strategy  →  Task Context  →  Output Format
```

**Key Methods**:
- `build_complexity_evaluation_prompt()` - For PENDING agents
- `build_boss_delegation_prompt()` - For BOSS decomposition
- `build_manager_decomposition_prompt()` - For MANAGER decomposition
- `build_security_benchmark_prompt()` - For security CVE tasks
- `build_auto_prompt()` - **Auto-detects task type** and routes

**Security Detection**:
```python
SECURITY_KEYWORDS = frozenset({
    "cve", "cwe", "vulnerability", "exploit", "poc",
    "proof-of-concept", "sanitizer", "addresssanitizer",
    ...
})

def is_security_task(task_description: str) -> bool:
    lower_desc = task_description.lower()
    return any(keyword in lower_desc for keyword in SECURITY_KEYWORDS)
```

---

## Ports Layer

**Location**: `core/ports/`

Abstract interfaces (Python Protocols) that define contracts for external systems.

### 1. EventStorePort (`event_store_port.py`)

```python
class EventStorePort(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def initialize_schema(self) -> None: ...

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append with OCC. Raises ConcurrencyError if version mismatch."""

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]: ...
    async def get_all_aggregate_ids(self) -> list[UUID]: ...
```

**OCC (Optimistic Concurrency Control)**:
- Every write includes `expected_version`
- If actual version differs, `ConcurrencyError` is raised
- Caller must retry with fresh state

### 2. LLMPort (`llm_port.py`)

```python
class LLMPort(Protocol):
    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Simple query, no cost tracking."""

    async def query_with_usage(self, prompt: str, config_dict: dict[str, Any]) -> LLMResponse:
        """Query with token usage and cost metadata."""
```

**LLMResponse Structure**:
```python
@dataclass(frozen=True)
class LLMResponse:
    content: str
    usage: LLMUsage  # prompt_tokens, completion_tokens, total_tokens
    model: str
    cost_usd: float
```

### 3. WorkerToolPort (`worker_port.py`)

```python
class WorkerToolPort(Protocol):
    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Execute task, yield progress events."""
```

Yields events like:
- `ThoughtCaptured` - Tool thinking/output
- `WorkCompleted` - Success
- `WorkFailed` - Failure

---

## Infrastructure Layer

**Location**: `infrastructure/adapters/`

Concrete implementations of ports.

### 1. PostgresEventStore (`postgres_event_store.py`)

Implements `EventStorePort` with PostgreSQL:

```sql
CREATE TABLE events (
    event_id UUID PRIMARY KEY,
    aggregate_id UUID NOT NULL,
    sequence_number INTEGER NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    event_data JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    UNIQUE (aggregate_id, sequence_number)  -- OCC enforced by DB
);
```

**OCC Implementation**:
```python
async def append(self, event: DomainEvent, expected_version: int) -> None:
    # Check version before insert
    current = await self._get_max_sequence(event.aggregate_id)
    if current != expected_version:
        raise ConcurrencyError(...)

    # Insert event
    await self._insert_event(event)
```

### 2. LiteLLMAdapter (`litellm_adapter.py:18-210`)

Implements `LLMPort` using LiteLLM (unified interface to OpenAI, Anthropic, etc.):

```python
class LiteLLMAdapter(LLMPort):
    async def query_with_usage(self, prompt: str, config_dict: dict[str, Any]) -> LLMResponse:
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Extract usage and calculate cost
        cost_usd = self._calculate_cost(model, prompt_tokens, completion_tokens, response)

        return LLMResponse(content=content, usage=usage, model=model, cost_usd=cost_usd)
```

### 3. ClaudePTYAdapter (`claude_pty_adapter.py`)

Implements `WorkerToolPort` using Claude Code CLI via PTY:

- Spawns `claude` subprocess
- Captures thinking (extended thinking blocks)
- Parses output for progress/completion
- Yields `ThoughtCaptured`, `WorkCompleted`, `WorkFailed` events

### 4. OpenHandsAdapter (`openhands_adapter.py`)

Implements `WorkerToolPort` using OpenHands headless mode:

- Runs OpenHands in Docker
- Captures agent actions
- Yields domain events

---

## Application Layer

**Location**: `core/application/`

Orchestrates the agent lifecycle.

### AgentExecutionService (`execution_service.py:43-491`)

**"The Brain"** - coordinates everything:

```python
class AgentExecutionService:
    def __init__(
        self,
        event_store: EventStorePort,    # Persistence
        llm_port: LLMPort,               # LLM queries
        worker_tool_port: WorkerToolPort, # Task execution
        system_limits: LimitsConfig,      # max_depth, max_children, etc.
        budget_config: BudgetConfig,      # Cost limits
        ...
    ): ...
```

**Core Loop** (`run_system_loop`):
```python
async def run_system_loop(self, root_agent_id: UUID) -> None:
    while True:
        active_agents = await self._get_active_agent_ids()
        if not active_agents:
            break

        for agent_id in active_agents:
            await self.run_agent_step(agent_id)

        await asyncio.sleep(self.poll_interval)
```

**Agent Step** (`run_agent_step`):
```
Load Agent → Check Budget → Dispatch Action → Persist Events → Handle Children → Notify Parent
```

**Dispatch Logic** (`_dispatch_agent_action`):
```python
if agent.role == AgentRole.PENDING:
    await agent.evaluate_complexity(self.llm_port, self.prompt_builder)
elif agent.role in (AgentRole.BOSS, AgentRole.MANAGER):
    await agent.evaluate_task(self.llm_port, self.prompt_builder)
elif agent.role == AgentRole.WORKER:
    async with self._worker_semaphore:  # Rate limiting
        await agent.execute_task(self.worker_tool_port, ...)
```

**Budget Enforcement**:
```python
def _track_cost(self, event: DomainEvent) -> None:
    if isinstance(event, TokensConsumed):
        self._total_cost_usd += event.cost_usd

        if self._total_cost_usd >= warning_threshold:
            print("⚠️ Budget warning: ...")

def _is_budget_exceeded(self) -> bool:
    return self._total_cost_usd >= self.budget_config.max_total_cost_usd
```

**Limit Enforcement**:
- `max_depth` - Forces WORKER role at max depth (prevents infinite recursion)
- `max_children_per_node` - Truncates subtask list
- `max_total_agents` - Skips child creation when exceeded
- `max_concurrent_workers` - Semaphore limits parallel executions

---

## Security Benchmark Feature

**NEW**: Auto-detection of CVE/security tasks with specialized prompts.

### How It Works

1. **Detection** (`prompt_builder.py:36-46`):
```python
def is_security_task(task_description: str) -> bool:
    keywords = {"cve", "cwe", "vulnerability", "exploit", "poc", ...}
    return any(keyword in task_description.lower() for keyword in keywords)
```

2. **Routing** (`prompt_builder.py:256-308`):
```python
def build_auto_prompt(self, task_description, agent_id, agent_role, ...):
    is_security = is_security_task(task_description)

    if agent_role == "BOSS" and is_security:
        return self.build_security_benchmark_prompt(...)
    if agent_role == "MANAGER" and is_security:
        return self.build_security_manager_prompt(...)
    # ... standard prompts for non-security
```

3. **Integration** (`model.py:171-191`):
```python
async def evaluate_task(self, llm_port, prompt_builder):
    # Uses auto_prompt for automatic security task detection
    prompt = prompt_builder.build_auto_prompt(
        task_description=self.task_description,
        agent_id=self.session_id,
        agent_role=self.role.value.upper(),
    )
```

### Security Prompt Templates

| Template | Purpose |
|----------|---------|
| `security/role_boss_security.j2` | BOSS role for CVE orchestration |
| `security/strategy_cve_benchmark.j2` | CVE analysis with CWE-specific guidance |
| `security/manager_security.j2` | MANAGER decomposition for security tasks |
| `security/worker_poc_generation.j2` | PoC generation strategies by CWE type |
| `security/worker_patch_generation.j2` | Patch generation patterns |
| `security/output_format_benchmark.j2` | JSON schema for security subtasks |

### Configuration (`config/config.yaml`)

```yaml
security:
  enabled: true
  auto_detect: true                          # Auto-detect from keywords
  default_model_poc: gpt-4o                  # Creative PoC generation
  default_model_patch: claude-3-5-sonnet-20241022  # Accurate patching
  default_model_validation: gpt-4o-mini      # Deterministic validation
  poc_temperature: 0.6                       # Higher for creativity
  patch_temperature: 0.5                     # Balanced
  validation_temperature: 0.2                # Low for consistency
  max_poc_attempts: 3
  max_patch_attempts: 3
  docker_timeout: 300
  sanitizer_flags: "-fsanitize=address,undefined -g"
```

### Example Security Task Flow

```
Input: "Generate PoC and patch for CVE-2024-1234 heap overflow"

BOSS (security mode)
  │ Uses strategy_cve_benchmark.j2
  │ Decomposes into 2-3 subtasks
  ▼
MANAGER (security mode)
  │ Uses manager_security.j2
  │ Further refines subtasks
  ▼
WORKER (PoC)              WORKER (Patch)
  │ worker_poc_generation.j2    │ worker_patch_generation.j2
  │ Generates exploit           │ Generates fix
  ▼                             ▼
[AddressSanitizer validation]  [Compilation validation]
```

---

## Key Code Paths

### 1. Task Execution Flow

```
CLI run "task"
    ↓
create_boss_agent(task)           # Creates BOSS, emits AgentCreated + TaskAssigned
    ↓
run_system_loop()
    ↓
run_agent_step(boss_id)
    ↓
_dispatch_agent_action()
    ↓
agent.evaluate_task()             # LLM decomposes task
    ↓
SubtasksDefined + ChildSpawned events
    ↓
_handle_child_spawning()          # Creates child agents
    ↓
[loop continues for children]
    ↓
WORKER executes → WorkCompleted
    ↓
_handle_parent_notification()     # ChildCompleted to parent
    ↓
[all children done] → parent WorkCompleted
```

### 2. OCC Retry Flow

```
run_agent_step()
    ↓
load events → reconstruct agent
    ↓
dispatch action → generate new events
    ↓
append(event, expected_version)
    ↓
  ┌─ Version matches → Success
  └─ Version mismatch → ConcurrencyError
                            ↓
                        retry_count++
                            ↓
                        reload & retry
```

---

## Testing Patterns

### Given-When-Then

```python
def test_boss_decomposes_task():
    # Given
    boss = AgentSession.create(session_id=uuid4(), role=AgentRole.BOSS, config={})
    boss.assign_task("Build a REST API")

    # When
    await boss.evaluate_task(mock_llm, prompt_builder)

    # Then
    assert any(isinstance(e, SubtasksDefined) for e in boss.events)
    assert any(isinstance(e, ChildSpawned) for e in boss.events)
```

### Event Replay

```python
def test_state_reconstruction():
    # Given
    events = [
        AgentCreated(aggregate_id=id, sequence_number=1, role="boss"),
        TaskAssigned(aggregate_id=id, sequence_number=2, task_description="Test"),
        WorkCompleted(aggregate_id=id, sequence_number=3, result="Done"),
    ]

    # When
    agent = AgentSession.load_from_history(events)

    # Then
    assert agent.status == AgentStatus.COMPLETED
    assert agent.result == "Done"
```

---

## Quick Reference

| Concept | Location | Line |
|---------|----------|------|
| AgentSession (aggregate) | `core/domain/model.py` | 60 |
| Event definitions | `core/domain/events.py` | 16 |
| PromptBuilder | `core/domain/prompt_builder.py` | 49 |
| Security detection | `core/domain/prompt_builder.py` | 36 |
| EventStorePort | `core/ports/event_store_port.py` | 9 |
| LLMPort | `core/ports/llm_port.py` | 8 |
| WorkerToolPort | `core/ports/worker_port.py` | 9 |
| ExecutionService | `core/application/execution_service.py` | 43 |
| LiteLLM adapter | `infrastructure/adapters/litellm_adapter.py` | 18 |
| Settings | `config/settings.py` | 153 |
