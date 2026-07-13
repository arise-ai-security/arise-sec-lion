<!-- Read this when: implementing features, adding events/adapters, or following workflow recipes -->
Recurring code patterns and step-by-step recipes for adding features to the arise-sec-lion codebase.

> **STOP: Run `cat .claude/docs/conventions.md` and read it before continuing.**

---

# Patterns

## Pattern 1: Event Sourcing on AgentSession

All state changes flow through: domain method on `AgentSession` calls `_emit()` which calls `_apply()` (singledispatchmethod) then appends to `_changes`. State is always derived from replaying events -- never set directly.

Events are frozen Pydantic models inheriting from `DomainEvent`. The base class deep-copies all dict/list fields via a `model_validator(mode="before")`, so subclasses do not need per-field copy logic.

```python
# core/domain/events/events.py
class MyNewEvent(DomainEvent):
    """What state change this represents and when it is emitted."""

    my_field: str
    optional_data: dict[str, Any] = Field(default_factory=dict)
```

The `_apply` handler uses `@singledispatchmethod` registration on `AgentSession`:

```python
# core/domain/aggregates/agent_session.py
@_apply.register
def _(self, event: MyNewEvent) -> None:
    self.my_field = event.my_field
    self.version += 1  # Always increment for OCC
```

Domain methods create the event, then call `self._emit()`:

```python
def do_something(self, my_field: str) -> None:
    event = MyNewEvent(
        aggregate_id=self.agent_id,
        sequence_number=self._next_sequence(),
        my_field=my_field,
    )
    self._emit(event)
```

Key rules:
- `model_config = {"frozen": True}` is inherited from `DomainEvent` -- do not redeclare it on subclasses.
- Always increment `self.version += 1` in every `_apply` handler (required for optimistic concurrency).
- Observability-only events (e.g., `ThoughtCaptured`, `PromptSent`) still need `_apply` handlers that increment version.
- The method is named `_emit`, not `_apply_and_record`.

## Pattern 2: Port / Adapter

Ports are `Protocol` classes in `core/ports/`. Adapters are concrete implementations in `infrastructure/adapters/`. Bootstrap wires them together -- this is the only place both are imported.

Port protocols live in two files: `event_store_port.py` (segregated into Connect/Write/Read per ISP) and `runtime_ports.py` (everything else: `LLMPort`, `WorkerToolPort`, `SharedContextPort`, `ReconToolPort`, `SiblingViewPort`, `Toolset`).

```python
# core/ports/runtime_ports.py
class MyPort(Protocol):
    async def do_thing(self, input: str) -> str: ...
```

```python
# infrastructure/adapters/my_adapter.py
class MyAdapter:
    async def do_thing(self, input: str) -> str:
        try:
            return await external_call(input)
        except ExternalError as e:
            raise LLMError(f"Failed: {e}", original_error=e) from e
```

Adapters must wrap external exceptions into domain exceptions with `from e` chaining. Never leak third-party exception types into the domain.

## Pattern 3: TemplateChain (Prompt Building)

`TemplateChain` in `core/application/services/prompt/prompt_builder.py` is a fluent builder that assembles multi-tier prompts from Jinja2 templates. Templates live in `prompts/` with a 4-tier hierarchy:

```
prompts/
  system.j2              # Tier 1: global constraints
  roles/                  # Tier 2: per-role persona
    boss.j2, manager.j2, worker.j2, pending.j2
  operations/             # Tier 3: per-operation format
    assess.j2, decomposition.j2, execution.j2
  domains/                # Tier 4: optional domain-specific
    secbench/*.j2
  context/                # Conditional context blocks
    scope.j2, sibling.j2
```

Usage from `PromptBuilder`:

```python
chain = (
    self.chain()
    .render("system.j2", default_tool=self.default_tool, agent_role="worker")
    .render("roles/worker.j2")
    .render_if(handoff, "context/sibling.j2", **sibling_ctx)
    .text_if(user_prompt, user_prompt)
    .render("operations/execution.j2", task_description=task, briefing=briefing)
)
prompt = chain.build(separator="\n\n")
```

API:
- `render(template, **kwargs)` -- required, raises `TemplateNotFound` if missing
- `render_if(condition, template, **kwargs)` -- renders only when condition is truthy
- `render_optional(template, **kwargs)` -- silently skips if template does not exist
- `text(content)` / `text_if(condition, content)` -- inject raw text
- `build(separator="\n\n")` -- join all accumulated parts

## Pattern 4: Discriminated Union (NodeMessage)

Inter-agent messages use a Pydantic discriminated union keyed on `direction` field. All variants are frozen. Three directions: `Briefing` (down), `Report` (up), `Handoff` (lateral). See `core/domain/values/node_message.py`.

```python
# Each variant has a unique direction literal:
class Briefing(BaseModel):
    model_config = {"frozen": True}
    direction: Literal["down"] = "down"
    parent_task: str
    # ...

# Union assembled via Pydantic Discriminator + Tag:
NodeMessage = Annotated[
    Annotated[Briefing, Tag("down")] | Annotated[Report, Tag("up")]
    | Annotated[Handoff, Tag("lateral")],
    Discriminator(_direction_discriminator),
]
```

To add a new direction: create a frozen `BaseModel` with a unique `direction: Literal[...]`, add it to the union, and add a corresponding `Tag`.

## Pattern 5: Plugin Protocol (DomainPlugin)

Domain-specific behavior is injected via the `DomainPlugin` protocol in `core/ports/domain_plugin_port.py`. The protocol defines hooks for context inference, prompt enrichment, run/worker workspace preparation, and metadata.

Core remains domain-ignorant -- it passes `domain_context: object | None` as an opaque slot through `HierarchyLimits`. Only the plugin downcasts.

```python
# plugins/security/plugin.py
class SecurityDomainPlugin(DomainPlugin):
    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        cve_file = kwargs.get("context_file") or kwargs.get("cve_file")
        if isinstance(cve_file, (str, Path)):
            return CVEInstance.from_json_file(cve_file)
        return self._inference_service.infer_instance(task_text)

    def get_prompt_strategy(self) -> PromptStrategy | None:
        return SecBenchPromptStrategy()
```

Bootstrap is the sole cross-boundary import point:

```python
# bootstrap/composition.py
from plugins.security import SecurityDomainPlugin

_DOMAIN_COMPONENT_BUILDERS: dict[str, Callable[[Settings], DomainComponents]] = {
    "security": _build_security_components,
}
```

## Pattern 6: CQRS Projection Pipeline

Read-side projections in `core/query/projections/` follow a pipeline: EventStore -> HierarchyCollector -> Filter -> Formatter -> Sink. Components self-register via decorators from `core/query/projections/registry.py`:

```python
@register_filter("errors_only")
class ErrorOnlyFilter:
    def matches(self, event: DomainEvent) -> bool:
        return isinstance(event, WorkFailed)
```

`ProjectionPipelineBuilder` assembles pipelines fluently:

```python
pipeline = (
    ProjectionPipelineBuilder(event_store)
    .with_filter("errors_only").with_output("summary")
    .with_formatter("json").to_sink("stdout").build()
)
await pipeline.execute(root_agent_id)
```

## Pattern 7: Configuration Hierarchy

Config loads via `Settings.load()` in `config/settings.py`: `config/config.yaml` (base) <- `config/config.{ARISE_ENV}.yaml` (overlay) <- env vars (highest priority).

New config sections: add a Pydantic `BaseModel` on `Settings`, then set defaults in YAML:

```python
# config/settings.py                    # config/config.yaml
class MyConfig(BaseModel):              # my_config:
    enabled: bool = False               #   enabled: true
    threshold: int = 10                 #   threshold: 20

class Settings(BaseSettings):
    my_config: MyConfig = Field(default_factory=MyConfig)
```

---

# Workflow Recipes

## Recipe: Adding a New Domain Event

**Files to touch:** `core/domain/events/events.py`, `infrastructure/adapters/postgres_event_store.py` (`EVENT_TYPE_REGISTRY`), `core/domain/aggregates/agent_session.py`, `core/domain/tests/`

> There are TWO event-sourced aggregates. Events that mutate `SharedStore` (not `AgentSession`)
> get their `_apply` handler in `core/domain/shared_context.py` instead.

1. Define the frozen event class in `core/domain/events/events.py`:

```python
class TaskPaused(DomainEvent):
    """Agent execution paused by external signal."""

    reason: str
    resume_after_seconds: int | None = None
```

2. Register the class in `EVENT_TYPE_REGISTRY` in `infrastructure/adapters/postgres_event_store.py`. Without this the event persists but silently fails to deserialize when replayed from Postgres.

3. Import it in `core/domain/aggregates/agent_session.py` and add the `_apply` handler:

```python
from core.domain.events.events import TaskPaused

@_apply.register
def _(self, event: TaskPaused) -> None:
    self.status = AgentStatus.PAUSED  # if adding a new status
    self.version += 1
```

4. Add a domain method that emits the event:

```python
def pause(self, reason: str, resume_after: int | None = None) -> None:
    event = TaskPaused(
        aggregate_id=self.agent_id,
        sequence_number=self._next_sequence(),
        reason=reason,
        resume_after_seconds=resume_after,
    )
    self._emit(event)
```

5. Write Given-When-Then tests in `core/domain/tests/` -- one for the state transition, one for replay:

```python
def test_pause_transitions_to_paused() -> None:
    # Given: an agent in ANALYZING state
    agent_id = uuid4()
    config = {"strategy": "heuristic", "base": {"model": "gpt-4"}, "tool": "claude_code"}
    agent = AgentSession.create(agent_id=agent_id, role=AgentRole.BOSS, config=config)
    agent.assign_task("Build something")

    # When: the agent is paused
    agent.pause("external signal")

    # Then: status transitions and event is recorded
    assert agent.status == AgentStatus.PAUSED
    assert isinstance(agent.events[-1], TaskPaused)

def test_pause_survives_replay() -> None:
    # Given: an agent that was paused
    agent = AgentSession.create(agent_id=uuid4(), role=AgentRole.BOSS, config=config)
    agent.pause("maintenance")

    # When: replayed from history
    restored = AgentSession.load_from_history(agent.events)

    # Then: state matches
    assert restored.status == AgentStatus.PAUSED
```

## Recipe: Adding a New Port/Adapter

**Files to touch:** `core/ports/runtime_ports.py`, `infrastructure/adapters/`, `bootstrap/infrastructure.py`, `bootstrap/application.py`

> **STOP: Run `cat .claude/docs/architecture.md` and read it before continuing.**

1. Define the protocol in `core/ports/runtime_ports.py`:

```python
class NotificationPort(Protocol):
    """Send notifications to external systems."""

    async def notify(self, channel: str, message: str) -> None: ...
```

2. Implement the adapter in `infrastructure/adapters/notification_adapter.py`:

```python
import logging

from core.ports.runtime_ports import NotificationPort

logger = logging.getLogger(__name__)

class SlackNotificationAdapter:
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def notify(self, channel: str, message: str) -> None:
        try:
            await self._post(channel, message)
        except httpx.HTTPError as e:
            raise NotificationError(f"Slack delivery failed: {e}") from e
```

3. Add to `Infrastructure` dataclass and factory in `bootstrap/infrastructure.py`:

```python
@dataclass
class Infrastructure:
    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort
    shared_context: SharedContextPort
    recon_tool: ReconToolPort
    notification: NotificationPort  # Add here

def get_infrastructure(config: InfrastructureConfig) -> Infrastructure:
    # ...
    notification = SlackNotificationAdapter(config.notification_webhook)
    return Infrastructure(..., notification=notification)
```

4. Inject into services via `bootstrap/application.py`:

```python
orchestrator = AgentOrchestrator(
    ...,
    notification_port=infrastructure.notification,
)
```

## Recipe: Adding a New API Endpoint

**Files to touch:** `query/api/routes/`, `query/api/schemas.py`, `query/api/app.py`

1. If adding to an existing route group, add the endpoint to the relevant file in `query/api/routes/`. If creating a new group, create a new file.

```python
# query/api/routes/agents.py (existing file)
@router.get("/{agent_id}/metrics", response_model=AgentMetricsSchema)
async def get_agent_metrics(
    agent_id: UUID,
    event_store: EventStoreDep,
) -> AgentMetricsSchema:
    """Get metrics for an agent."""
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    projection = MetricsProjection()
    metrics = projection.project(events)
    return _metrics_to_schema(metrics)
```

2. Define the response schema in `query/api/schemas.py`:

```python
class AgentMetricsSchema(BaseModel):
    agent_id: str
    total_tokens: int
    cost_usd: float
```

3. If creating a new route group, register it in `query/api/app.py`:

```python
from query.api.routes import agents, metrics  # new import

app.include_router(metrics.router, prefix="/api/metrics", tags=["metrics"])
```

4. Use `EventStoreDep` (the read-only `EventStoreReadPort` alias from `query/api/dependencies.py`) for query endpoints. Use `ExecutionServiceDep` only for command endpoints. Use `DomainPluginDep` when endpoint needs domain-specific behavior.

## Recipe: Adding a New Worker Tool Adapter

**Files to touch:** `infrastructure/adapters/worker/`, `infrastructure/adapters/worker/__init__.py`, `bootstrap/infrastructure.py`

Worker tool adapters extend `WorkerAdapterBase` (which implements `WorkerToolPort`) and yield `DomainEvent` instances via `async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]`.

1. Create the adapter in `infrastructure/adapters/worker/my_tool_adapter.py`. Extend `WorkerAdapterBase` and implement the two abstract methods:

```python
# infrastructure/adapters/worker/my_tool_adapter.py
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.domain.events.events import DomainEvent, ThoughtCaptured
from .base import WorkerAdapterBase
from .shared import EventSequencer

@dataclass
class MyToolConfig:
    model: str | None = None
    timeout_seconds: int = 300

class MyToolAdapter(WorkerAdapterBase):
    STREAM_NAME = "my_tool"  # Required: used by EventSequencer

    def __init__(self, config: MyToolConfig | None = None) -> None:
        self._config = config or MyToolConfig()
        super().__init__(timeout_seconds=self._config.timeout_seconds)

    def _get_tool_name(self) -> str:
        return "my_tool"

    async def _execute_task(
        self, task_description: str, agent_id: UUID,
        working_dir: str, sequencer: EventSequencer, **kwargs: Any,
    ) -> AsyncIterator[DomainEvent]:
        async for chunk in self._stream_from_tool(task_description):
            yield ThoughtCaptured(
                aggregate_id=agent_id,
                sequence_number=0,  # Corrected by AgentSession.apply_worker_event
                content=chunk,
            )
```

2. Export from `infrastructure/adapters/worker/__init__.py`:

```python
from .my_tool_adapter import MyToolAdapter, MyToolConfig
```

3. Add a branch in `_create_worker_adapter()` in `bootstrap/infrastructure.py`:

```python
if config.default_worker_tool == "my_tool":
    return MyToolAdapter(MyToolConfig(
        model=config.worker_tool_model,
        timeout_seconds=config.worker_tool_timeout,
    ))
```

4. Add the literal to the `WorkerToolType` type alias in `bootstrap/infrastructure.py`:

```python
type WorkerToolType = Literal["claude_code", "openhands", "google_adk", "my_tool"]
```

Worker events use `sequence_number=0` as a placeholder. `AgentSession.apply_worker_event()` corrects it with the real sequence before persisting.

## Recipe: Adding a New Domain Plugin

**Files to touch:** `plugins/<name>/`, `bootstrap/composition.py`

> **STOP: Run `cat .claude/docs/plugins-security.md` and read it before continuing.**

1. Create `plugins/my_domain/__init__.py` and `plugins/my_domain/plugin.py`. Implement every method on the `DomainPlugin` protocol (see `core/ports/domain_plugin_port.py` for the full signature list):

```python
# plugins/my_domain/plugin.py
from core.ports.domain_plugin_port import DomainPlugin, PreparedRunWorkspace, WorkerExecutionContext

class MyDomainPlugin(DomainPlugin):
    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        return MyDomainContext.from_text(task_text)

    def enrich_prompt(self, prompt: str, *, domain_context: object | None,
                      briefing: object | None = None,
                      chain_factory: Callable[[], object] | None = None) -> str:
        if domain_context is None:
            return prompt
        return f"{prompt}\n\n<domain_context>\n{domain_context}\n</domain_context>"

    def get_run_metadata(self, domain_context: object) -> dict: return {}
    def get_tag_mappings(self) -> dict: return {}
    def get_provenance_patterns(self) -> list: return []
    def get_prompt_strategy(self) -> None: return None
    def get_procedure_executor(self) -> None: return None  # None => deterministic tier disabled
    async def prepare_run(self, **kwargs) -> PreparedRunWorkspace | None: return None
    async def prepare_worker_execution(self, **kwargs) -> WorkerExecutionContext | None: return None
    async def cleanup_worker_execution(self, **kwargs) -> None: pass
```

2. Register the builder in `bootstrap/composition.py` (the only file that imports from `plugins/`):

```python
from plugins.my_domain import MyDomainPlugin

def _build_my_domain_components(settings: Settings) -> DomainComponents:
    if not settings.my_domain.enabled:
        return DomainComponents()
    plugin = MyDomainPlugin()
    return DomainComponents(
        plugin=plugin,
        prompt_strategy=plugin.get_prompt_strategy(),
        domain_key="my_domain",
    )

_DOMAIN_COMPONENT_BUILDERS: dict[str, Callable[[Settings], DomainComponents]] = {
    "security": _build_security_components,
    "my_domain": _build_my_domain_components,  # Add here
}
```

Only `bootstrap/composition.py` may import from `plugins/`. No other layer references plugin code directly.

## Recipe: Adding a Procedure (Deterministic Dispatch Tier)

**Files to touch:** `plugins/<domain>/` (an executor), `bootstrap/application.py` (already wired). No `core/` change needed -- the port already exists.

> **STOP: Run `cat .claude/docs/plugins-security.md` and read it before continuing.**

Use this when a subtask is a *fixed procedure* (deterministic, host-computable) rather than open-ended agentic work -- e.g. a validator that just re-runs a benchmark harness. It runs host-side with zero LLM turns and event-sources agent-unforgeable evidence.

1. Implement `ProcedureExecutorPort` (`core/ports/procedure_ports.py`) in your plugin. `match` maps a task to a `procedure_ref`; `resolve` validates a ref; `execute` runs it and returns a `ProcedureResult` (`success`, `summary`, `digest`, `evidence`). Return `success=False` + a `digest` for task-level failure; raise only for infrastructure faults.

```python
# plugins/my_domain/procedures.py
from core.domain.values.procedure import ProcedureEvidence, ProcedureResult

class MyProcedureExecutor:  # structural: satisfies ProcedureExecutorPort
    def match(self, task_description: str, domain_context: object | None) -> str | None: ...
    def resolve(self, procedure_ref: str) -> bool: ...
    async def execute(self, procedure_ref, task_description, domain_context, params) -> ProcedureResult: ...
```

2. Return it from your plugin's `get_procedure_executor()` (return `None` to keep the tier off). `bootstrap/application.py` binds it as the run's executor **only when** `settings.orchestration.procedural_dispatch` is on; otherwise `NullProcedureExecutor` keeps behavior byte-identical.

3. Register the trusted role bracket in the plugin executor's `match()` implementation. At runtime the Host calls `match(task, domain_context)` and then `resolve()`; parent-authored `Subtask.execution_mode`, `procedure_ref`, and `procedure_params` are provenance only and never dispatch authority. A failed procedure escalates to exactly one agentic retry carrying the procedure's digest.

**Do NOT** add non-security procedures to `plugins/security/`. Procedures are domain-specific worker behavior, so they live in a domain plugin -- never in `core/` or `infrastructure/`.

## Recipe: Adding a New CQRS Projection

**Files to touch:** `core/query/projections/`, optionally `infrastructure/adapters/sinks.py`

The projection pipeline has four extensible component types, each registered via decorators from `core/query/projections/registry.py`:

| Component | Protocol | Register with | Location |
|---|---|---|---|
| Filter | `EventFilter` in `core/query/projections/base/filter.py` | `@register_filter("name")` | `core/query/projections/filters/impl.py` |
| Formatter | `Formatter` in `core/query/projections/base/formatter.py` | `@register_formatter("name")` | `core/query/projections/formatters/` |
| Projection | `Projection` in `core/query/projections/base/projection.py` | `@register_projection("name")` | `core/query/projections/impl/` |
| Sink | `SinkPort` in `core/query/ports/sink_port.py` | `@register_sink("name")` | `infrastructure/adapters/sinks.py` |

1. To add a new filter:

```python
# core/query/projections/filters/impl.py
from core.domain.events.events import DomainEvent, TokensConsumed, WorkerCostRecorded
from core.query.projections.registry import register_filter

@register_filter("cost_only")
class CostOnlyFilter:
    """Pass only cost-related events."""

    def matches(self, event: DomainEvent) -> bool:
        return isinstance(event, (TokensConsumed, WorkerCostRecorded))
```

2. To add a new sink:

```python
# infrastructure/adapters/sinks.py
@register_sink("webhook")
class WebhookSink:
    def __init__(self, url: str) -> None:
        self._url = url

    def write(self, content: str) -> None:
        httpx.post(self._url, content=content)
```

3. Ensure new sink modules are imported at bootstrap time. The file `infrastructure/adapters/sinks.py` is imported in `bootstrap/infrastructure.py` via `import infrastructure.adapters.sinks as _sinks` specifically to trigger decorator registration.

4. Re-export new filters from `core/query/projections/filters/__init__.py` so other modules can import them.

---

# Anti-Patterns

## Never import infrastructure in core

Pre-commit hook `scripts/check_architecture_boundaries.py` blocks this. No file under `core/` may import from `infrastructure/`.

```python
# WRONG -- in any file under core/
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter

# RIGHT -- depend on the protocol
from core.ports.runtime_ports import LLMPort
```

## Never mutate aggregate state directly

State changes must go through `_emit` -> `_apply`. Direct mutation bypasses event sourcing and breaks replay.

```python
# WRONG
agent.status = AgentStatus.COMPLETED

# RIGHT
agent.complete_work(result="done")  # emits WorkCompleted, _apply sets status
```

## Never put non-security code in plugins/

Topology, orchestration, scheduling, and generic domain logic belong in `core/`. The `plugins/` directory is strictly for cybersecurity code.

## Never use mutable Pydantic models for events or value objects

All domain events inherit `frozen=True` from `DomainEvent`. Value objects must explicitly declare it.

```python
# WRONG
class Subtask(BaseModel):
    description: str

# RIGHT
class Subtask(BaseModel):
    model_config = {"frozen": True}
    description: str
```

## Never create pipeline/strategy abstractions for orchestrator operations

The three operations (`assess_task`, `evaluate_task`, `execute_task`) on `AgentOrchestrator` are intentionally plain methods. Do not wrap them in a strategy pattern, pipeline, or chain-of-responsibility.

## Never use print() for output

Use `logging.getLogger(__name__)` or the presentation layer renderers. Direct `print()` calls bypass log configuration and structured output.

## Never swallow exceptions silently

```python
# WRONG
except Exception:
    pass

# RIGHT
except SpecificError as e:
    logger.exception("Context about what failed", extra={"agent_id": str(agent_id)})
    raise DomainError(f"Operation failed: {e}") from e
```
