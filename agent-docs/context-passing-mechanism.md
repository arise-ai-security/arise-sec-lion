# Context Passing Mechanism

This document describes how agents share state and context across the execution hierarchy using the SharedExecutionContext system.

> **See also:** [Context Passing Guide](context-passing-guide.md) for practical scenarios and implementation patterns.

---

## Overview

In a recursive multi-agent system, agents need to share information:
- **Artifacts**: Files, code snippets, analysis results produced by workers
- **Decisions**: Architectural choices, tool selections, design decisions
- **Progress**: Checkpoint status for long-running operations
- **Budget**: Cost tracking and enforcement across the hierarchy

The **SharedExecutionContext** provides an event-sourced, centralized store for cross-agent state sharing within a single execution run.

```
                    ┌─────────────────────────────────────┐
                    │       SharedExecutionContext        │
                    │         (one per root_id)           │
                    ├─────────────────────────────────────┤
                    │  ArtifactStore    │  DecisionLog    │
                    │  ProgressTracker  │  BudgetAccount  │
                    └─────────────────────────────────────┘
                                    ▲
                    ┌───────────────┼───────────────┐
                    │               │               │
                 BOSS           MANAGER          WORKER
                    │               │               │
                    └───────────────┴───────────────┘
                         All agents share access
```

---

## Key Concepts

### One Context Per Execution Hierarchy

Each execution run (starting from a BOSS agent) has exactly one SharedExecutionContext, identified by the `root_id`:

```python
# Derived aggregate ID prevents collision with AgentSession
aggregate_id = uuid5(SHARED_CONTEXT_NAMESPACE, str(root_id))
```

### Event-Sourced State

All state changes are captured as domain events, enabling:
- Complete audit trail of shared decisions and artifacts
- OCC (Optimistic Concurrency Control) for concurrent access
- Replay capability for debugging and recovery

---

## Component Aggregates

SharedExecutionContext is a **facade** composing four specialized aggregates:

### 1. ArtifactStore

Stores shared outputs between agents (files, code, analysis results).

```python
# Store an artifact
context.store_artifact(
    key="vulnerability_report",
    content_type="text/markdown",
    stored_by=worker_agent_id,
    content="## Findings\n- SQL Injection in login.php...",
)

# Retrieve artifact
artifact = context.get_artifact("vulnerability_report")
# Returns: Artifact(key, content_type, content, content_hash, stored_by, metadata)
```

**Events**: `ArtifactStored`

### 2. DecisionLog

Records architectural and design decisions with rationale.

```python
# Record a decision
context.record_decision(
    decision_key="target_framework",
    decision_value="django",
    rationale="Target app uses Django 3.2 based on requirements.txt",
    decided_by=manager_agent_id,
)

# Query decision
decision = context.get_decision("target_framework")
# Returns: Decision(key, value, rationale, decided_by)
```

**Events**: `DecisionRecorded`

### 3. ProgressTracker

Tracks progress checkpoints across the hierarchy.

```python
# Update progress
context.update_progress(
    checkpoint_key="exploit_development",
    status="in_progress",
    progress_pct=60.0,
    message="PoC working, testing payload variations",
    reported_by=worker_agent_id,
)

# Check progress
checkpoint = context.get_progress("exploit_development")
# Returns: ProgressCheckpoint(key, status, progress_pct, message, reported_by)
```

**Events**: `ProgressUpdated`

### 4. BudgetAccount

Tracks cost consumption and enforces budget limits.

```python
# Consume budget (called automatically by LLM adapter)
context.consume_budget(
    agent_id=agent_id,
    amount=0.0023,
    operation="llm_call",
    model="gpt-4",
    tokens={"input": 1500, "output": 200},
)

# Check budget
remaining = context.get_remaining_budget()  # -1 if unlimited
exceeded = context.is_budget_exceeded()
summary = context.get_budget_summary()
# Returns: {initial_budget_usd, consumed_budget_usd, remaining_budget_usd, budget_exceeded}
```

**Events**: `BudgetConsumed`, `BudgetExceeded`

---

## Domain Events

All SharedContext events (defined in `core/domain/events/events.py`):

| Event | Purpose | Key Fields |
|-------|---------|------------|
| `SharedContextCreated` | Context initialization | `root_id`, `initial_budget_usd`, `config` |
| `ArtifactStored` | Artifact added/updated | `key`, `content_type`, `content`, `stored_by` |
| `DecisionRecorded` | Decision logged | `decision_key`, `decision_value`, `rationale`, `decided_by` |
| `ProgressUpdated` | Progress checkpoint update | `checkpoint_key`, `status`, `progress_pct`, `reported_by` |
| `BudgetConsumed` | Cost recorded | `consumed_by`, `amount_usd`, `operation`, `model`, `tokens` |
| `BudgetExceeded` | Budget limit hit | `limit_usd`, `consumed_usd`, `triggered_by` |
| `ConfigOverrideSet` | Runtime config change | `config_key`, `config_value`, `set_by`, `scope` |

---

## Port Interface

The `SharedContextPort` (`core/ports/shared_context_port.py`) defines the interface:

```python
class SharedContextPort(Protocol):
    async def get_or_create(
        self, root_id: UUID, initial_budget_usd: float, config: dict
    ) -> SharedExecutionContext:
        """Get existing or create new context."""

    async def get(self, root_id: UUID) -> SharedExecutionContext | None:
        """Get context by root ID."""

    async def save(self, context: SharedExecutionContext, expected_version: int) -> None:
        """Save with OCC (raises ConcurrencyError on conflict)."""

    async def exists(self, root_id: UUID) -> bool:
        """Check if context exists."""
```

### Interface Segregation

For read-only access, use `ContextReaderPort`:

```python
class ContextReaderPort(Protocol):
    async def get_shared_context(self, root_id: UUID) -> SharedExecutionContext | None
    async def get_artifact(self, root_id: UUID, key: str) -> Artifact | None
    async def get_decision(self, root_id: UUID, key: str) -> Decision | None
```

---

## Usage Flow

### 1. Context Creation (Boss Agent)

When a BOSS agent is created, SharedExecutionContext is initialized:

```python
# execution_service.py
async def create_boss_agent(self, task_description: str) -> UUID:
    root_id = uuid4()

    # Create shared context for this run
    shared_context = await self._shared_context_port.get_or_create(
        root_id=root_id,
        config={},
    )
    await self._shared_context_port.save(shared_context, expected_version=0)

    # Create boss agent...
```

### 2. Worker Updates Context

When workers complete, they can record decisions and artifacts:

```python
# execution_service.py
async def _process_worker_context_updates(self, agent: AgentSession) -> None:
    # Parse structured output from worker result
    parsed = parse_context_update(agent.result)

    root_id = self._context_registry.get_root_id(agent.agent_id)
    context = await self._shared_context_port.get(root_id)
    current_version = context.version

    # Record decisions
    for decision in parsed.decisions:
        context.record_decision(
            decision_key=decision.key,
            decision_value=decision.value,
            rationale=decision.rationale,
            decided_by=agent.agent_id,
        )

    # Store artifacts
    for output in parsed.outputs:
        context.store_artifact(
            key=output.key,
            content_type="text/plain",
            stored_by=agent.agent_id,
            content=output.description,
        )

    # Persist with OCC
    if context.events:
        await self._shared_context_port.save(context, expected_version=current_version)
        context.mark_changes_as_committed()
```

### 3. Sibling Context for Sequential Workers

Workers receive context from completed siblings via `SiblingViewPort`:

```python
# execution_service.py
async def _dispatch_agent_action(self, agent: AgentSession) -> None:
    if agent.role == AgentRole.WORKER:
        # Build context from completed siblings
        sibling_view = await self._sibling_view_port.build_context(
            agent_id=agent.agent_id,
            parent_id=agent.parent_id,
            root_id=root_id,
        )

        await self._orchestrator.execute_task(
            agent,
            sibling_view=sibling_view,  # Passed to worker prompt
            ...
        )
```

---

## Concurrency Control

### Optimistic Concurrency Control (OCC)

SharedContext uses the same OCC pattern as AgentSession:

1. Load context (get current version)
2. Make changes (record events)
3. Save with expected_version
4. On conflict: `ConcurrencyError` raised, caller retries

```python
# OCC pattern
context = await shared_context_port.get(root_id)
current_version = context.version  # e.g., 5

context.record_decision(...)  # Adds event to uncommitted list

try:
    await shared_context_port.save(context, expected_version=current_version)
except ConcurrencyError:
    # Another process updated first - reload and retry
    pass
```

### Database Constraint

OCC is enforced by PostgreSQL unique constraint:

```sql
CONSTRAINT unique_aggregate_sequence UNIQUE (aggregate_id, sequence_number)
```

### When Locking is Needed

OCC works well for low-contention scenarios. For high-contention operations (e.g., task deduplication where multiple managers may try to register the same task), consider:

- **PostgreSQL Advisory Locks**: Database-level distributed locks
- **Retry with backoff**: Exponential backoff on ConcurrencyError

---

## Key Files

| File | Purpose |
|------|---------|
| `core/domain/shared_context.py` | Domain model (SharedExecutionContext, aggregates) |
| `core/domain/events/events.py` | SharedContext domain events |
| `core/ports/shared_context_port.py` | Port interface |
| `infrastructure/adapters/shared_context_adapter.py` | PostgreSQL adapter |
| `core/application/execution_service.py` | Worker context update processing |
| `core/application/services/sibling_context_builder.py` | Builds sibling context (SiblingView) |

---

## Value Objects

Immutable Pydantic models used by SharedContext:

```python
class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    content_type: str
    content: str | None
    content_hash: str | None
    stored_by: UUID
    metadata: dict[str, Any]

class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    value: str
    rationale: str
    decided_by: UUID

class ProgressCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    status: str
    progress_pct: float
    message: str
    reported_by: UUID
```

---

## Quick Reference

| Operation | Method | Event |
|-----------|--------|-------|
| Store artifact | `context.store_artifact(...)` | `ArtifactStored` |
| Record decision | `context.record_decision(...)` | `DecisionRecorded` |
| Update progress | `context.update_progress(...)` | `ProgressUpdated` |
| Consume budget | `context.consume_budget(...)` | `BudgetConsumed` |
| Set config | `context.set_config_override(...)` | `ConfigOverrideSet` |

---

## Composable Context API (ContextComposer)

The **ContextComposer** provides a fluent, programmatic API for composing prompt context. Unlike SharedExecutionContext which stores persistent state, ContextComposer builds ephemeral template context for prompt generation.

```
                ┌─────────────────────────────────────────────────────────┐
                │                  ContextComposer                        │
                │             (fluent builder API)                        │
                ├─────────────────────────────────────────────────────────┤
                │  .add(ParentSummary)     → parent_summary               │
                │  .add(SiblingResults)    → sibling_results              │
                │  .add(SharedDecisions)   → shared_decisions             │
                │  .add(ChildOutcomes)     → child_outcomes               │
                │  .add(AncestorData)      → ancestor_{label}             │
                │  .add(CustomContext)     → custom_{label}               │
                └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                              .build() → template vars dict
                                          │
                                          ▼
                              Jinja2 templates render context
```

### ContextData Protocol

All context types implement the `ContextData` protocol (`core/domain/values/context/base.py`):

```python
class ContextData(Protocol):
    @property
    def template_key(self) -> str:
        """Key used in Jinja2 template (e.g., 'parent_summary')."""
        ...

    def to_template_dict(self) -> dict[str, Any]:
        """Convert to dict for template rendering."""
        ...
```

### ContextComposer Usage

```python
from core.application.services.context_composer import ContextComposer
from core.domain.values.context import (
    ParentSummary, SiblingResults, SharedDecisions, ChildOutcomes
)

# Build context with fluent API
context = (
    ContextComposer()
    .add(ParentSummary(task="Fix auth bug", result="Found SQL injection"))
    .add(SiblingResults(siblings=(...)))
    .add_if(has_decisions, SharedDecisions(decisions=(...)))  # Conditional
    .add_optional(maybe_artifacts)  # None-safe
)

# Get template variables
template_vars = context.build()
# {"parent_summary": {...}, "sibling_results": {...}, ...}
```

### Context Data Types

#### Hierarchical Context (Parent/Ancestor)

| Type | Template Key | Purpose |
|------|--------------|---------|
| `ParentSummary` | `parent_summary` | Immediate parent's task, result, decisions |
| `AncestorData` | `ancestor_{label}` | Specific ancestor with custom label |
| `AncestryChain` | `ancestry_chain` | Full lineage from root to current |

```python
# Parent summary
ParentSummary(
    task="Analyze auth module",
    result="Found SQL injection vulnerability",
    decisions=("use_parameterized_queries",),
    role="manager",
)

# Labeled ancestor (template key: ancestor_fixer)
AncestorData(
    label="fixer",
    agent_id=fixer_id,
    role="manager",
    task="Apply security patch",
    result="Patch ready for review",
)
```

#### Horizontal Context (Siblings)

| Type | Template Key | Purpose |
|------|--------------|---------|
| `SiblingResults` | `sibling_results` | All siblings' status and results |
| `SiblingEntry` | (nested) | Single sibling data |

```python
SiblingResults(siblings=(
    SiblingEntry(
        agent_id="worker-123",
        index=0,
        status="completed",
        task_summary="Set up test environment",
        result_summary="Docker container on port 8080",
    ),
    SiblingEntry(
        agent_id="worker-456",
        index=1,
        status="in_progress",
        task_summary="Run exploit PoC",
    ),
))
```

#### Shared Context (From SharedExecutionContext)

| Type | Template Key | Purpose |
|------|--------------|---------|
| `SharedDecisions` | `shared_decisions` | Decisions from global context |
| `SharedArtifacts` | `shared_artifacts` | Artifacts from global context |

```python
SharedDecisions(decisions=(
    DecisionEntry(
        key="target_framework",
        value="django",
        rationale="Target uses Django 3.2",
    ),
))
```

#### Child Outcomes (Parent Viewing Children)

| Type | Template Key | Purpose |
|------|--------------|---------|
| `ChildOutcomes` | `child_outcomes` | Structured results from child agents |
| `ChildOutcomeEntry` | (nested) | Single child's task outcome |

```python
ChildOutcomes(outcomes=(
    ChildOutcomeEntry(
        child_id="worker-123",
        task_summary="Develop exploit PoC",
        result_text="Created poc.py with buffer overflow...",
        artifacts=("poc.py", "debug_log.txt"),
        decisions=("use_ret2libc",),
    ),
))
```

#### Custom Context

| Type | Template Key | Purpose |
|------|--------------|---------|
| `CustomContext` | `custom_{label}` | Arbitrary user-defined data |

```python
CustomContext(
    label="vulnerability_info",
    data={
        "cve_id": "CVE-2023-1234",
        "affected_function": "parse_input()",
    },
)
# Template access: {{ custom_vulnerability_info.cve_id }}
```

---

## Factory Functions

Factory functions (`core/application/services/context_factories.py`) convert domain objects to context types:

```python
from core.application.services.context_factories import (
    parent_summary_from_agent,
    sibling_results_from_agents,
    shared_decisions_from_context,
    child_outcomes_from_agent,
)

# From AgentSession
parent = await repository.load(agent.parent_id)
context.add(parent_summary_from_agent(parent))

# From list of siblings
siblings = await get_siblings(agent)
context.add(sibling_results_from_agents(siblings, exclude_id=agent.agent_id))

# From SharedExecutionContext
shared_ctx = await shared_context_port.get(root_id)
context.add(shared_decisions_from_context(shared_ctx))

# From parent's child outcomes
context.add(child_outcomes_from_agent(parent_agent))
```

### Available Factories

| Factory | Input | Output |
|---------|-------|--------|
| `parent_summary_from_agent` | `AgentSession` | `ParentSummary` |
| `ancestor_data_from_agent` | `AgentSession` + label | `AncestorData` |
| `ancestry_chain_from_agents` | `list[AgentSession]` | `AncestryChain` |
| `sibling_results_from_agents` | `list[AgentSession]` | `SiblingResults` |
| `sibling_entry_from_agent` | `AgentSession` | `SiblingEntry` |
| `shared_decisions_from_context` | `SharedExecutionContext` | `SharedDecisions` |
| `shared_artifacts_from_context` | `SharedExecutionContext` | `SharedArtifacts` |
| `child_outcomes_from_agent` | `AgentSession` | `ChildOutcomes` |

---

## Context Templates

Jinja2 templates in `prompts/core/context/` render composed context:

| Template | Renders | Context Type |
|----------|---------|--------------|
| `parent.j2` | Parent task/result/decisions | `ParentSummary` |
| `ancestry.j2` | Full ancestry chain | `AncestryChain` |
| `siblings.j2` | Sibling workers' status | `SiblingResults` |
| `decisions.j2` | Shared decisions | `SharedDecisions` |
| `artifacts.j2` | Shared artifacts | `SharedArtifacts` |
| `children.j2` | Child outcomes for parent | `ChildOutcomes` |
| `dynamic.j2` | Labeled ancestors + custom | `ancestor_*`, `custom_*` |

### Template Example (children.j2)

```jinja
{% if child_outcomes and child_outcomes.outcomes %}
<CHILD_OUTCOMES total="{{ child_outcomes.total_count }}" completed="{{ child_outcomes.completed_count }}">
{% for outcome in child_outcomes.outcomes %}
<child id="{{ outcome.child_id }}" status="{{ outcome.status }}">
<task>{{ outcome.task_summary }}</task>
<result>{{ outcome.result_text }}</result>
</child>
{% endfor %}
</CHILD_OUTCOMES>
{% endif %}
```

---

## Context Flow Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                       Context Sources                           │
├─────────────────────────────────────────────────────────────────┤
│  AgentSession        SharedExecutionContext       Custom Data   │
│  (parent, siblings)  (decisions, artifacts)       (user-defined)│
└─────────────────────┬─────────────────────────────┬─────────────┘
                      │                             │
                      ▼                             ▼
              Factory Functions              Direct Construction
              (context_factories.py)         (data_types.py)
                      │                             │
                      └──────────────┬──────────────┘
                                     ▼
                          ┌───────────────────────┐
                          │    ContextComposer    │
                          │  .add() / .add_if()   │
                          │  .add_optional()      │
                          │  .build()             │
                          └───────────┬───────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   Template Variables  │
                          │ {"parent_summary":... │
                          │  "sibling_results":...│
                          └───────────┬───────────┘
                                      ▼
                          ┌───────────────────────┐
                          │   Jinja2 Templates    │
                          │   (prompts/core/      │
                          │    context/*.j2)      │
                          └───────────┬───────────┘
                                      ▼
                          ┌───────────────────────┐
                          │    Rendered Prompt    │
                          │    (for LLM call)     │
                          └───────────────────────┘
```

---

## Key Files (Updated)

| File | Purpose |
|------|---------|
| `core/domain/shared_context.py` | Domain model (SharedExecutionContext, aggregates) |
| `core/domain/events/events.py` | SharedContext domain events |
| `core/domain/values/context/base.py` | ContextData protocol |
| `core/domain/values/context/data_types.py` | Concrete context types |
| `core/application/services/context_composer.py` | ContextComposer fluent builder |
| `core/application/services/context_factories.py` | Factory functions |
| `prompts/core/context/*.j2` | Context rendering templates |
| `core/ports/shared_context_port.py` | Port interface |
| `infrastructure/adapters/shared_context_adapter.py` | PostgreSQL adapter |
