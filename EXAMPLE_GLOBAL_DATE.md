# Example: Global Date Context for All Nodes

## Problem Statement

We want to add a global context of `current_date: 2025-12-31` (or today's date) and make all nodes in the system include this into their prompts. This ensures every agent in the hierarchy has awareness of the current date.

## Solution Overview

This example demonstrates how to use the **Context Passing Mechanism** to inject system-wide configuration into every agent's prompt. The solution introduces:

1. **`GlobalConfig`** - A new context data type for system-wide settings
2. **`GlobalConfigProvider`** - An application service that manages global configuration
3. **`InjectGlobalConfig`** - A pipeline step that adds global config to the ContextComposer
4. **Template rendering** - A Jinja2 template that renders global config into prompts

## Architecture

```
                ┌─────────────────────────────────────────────────────────┐
                │               GlobalConfigProvider                       │
                │          (Application Service Layer)                     │
                │    set_current_date(), set(key, value), etc.            │
                └─────────────────────┬───────────────────────────────────┘
                                      │
                                      ▼
                ┌─────────────────────────────────────────────────────────┐
                │                InjectGlobalConfig                        │
                │              (Pipeline Step Layer)                       │
                │    Converts GlobalConfigProvider → GlobalConfig          │
                │    Adds to ContextComposer                               │
                └─────────────────────┬───────────────────────────────────┘
                                      │
                                      ▼
                ┌─────────────────────────────────────────────────────────┐
                │                  ContextComposer                         │
                │        (carries GlobalConfig through pipeline)           │
                └─────────────────────┬───────────────────────────────────┘
                                      │
                                      ▼
                ┌─────────────────────────────────────────────────────────┐
                │              TemplateChain.with_context()                │
                │    Renders core/context/global.j2 → prompt text          │
                └─────────────────────────────────────────────────────────┘
```

## Code Changes

### 1. Domain Layer: GlobalConfig Data Type

**File:** `core/domain/values/context/data_types.py`

```python
class GlobalConfig(BaseModel):
    """Global configuration context available to all agents.

    Use for system-wide settings that every agent in the hierarchy
    should have access to. Examples include:
    - Current date/time for context awareness
    - Target system configuration
    - Global execution parameters
    """

    model_config = {"frozen": True}

    data: dict[str, Any]

    @property
    def template_key(self) -> str:
        return "global_config"

    def to_template_dict(self) -> dict[str, Any]:
        return self.data
```

### 2. Application Layer: GlobalConfigProvider Service

**File:** `core/application/services/global_config_provider.py`

```python
class GlobalConfigProvider:
    """Provides global configuration context to all agents."""

    def set(self, key: str, value: Any) -> "GlobalConfigProvider":
        """Set a configuration value."""
        self._data[key] = value
        return self

    def set_current_date(self, date_value: date | str | None = None) -> "GlobalConfigProvider":
        """Set the current date in the global config."""
        if date_value is None:
            date_value = date.today()
        # ... formats and stores date

    def to_global_config(self) -> GlobalConfig:
        """Convert to GlobalConfig context data for ContextComposer."""
        return GlobalConfig(data=self._data.copy())
```

### 3. Pipeline Layer: InjectGlobalConfig Step

**File:** `core/application/pipeline/steps/context.py`

```python
class InjectGlobalConfig:
    """Inject global configuration into the context composer."""

    def __init__(self, global_config_provider: "GlobalConfigProvider") -> None:
        self._provider = global_config_provider

    async def execute(self, state: PipelineState) -> StepResult:
        composer = state.context_composer or ContextComposer()
        if self._provider:
            composer.add(self._provider.to_global_config())
        return StepResult.ok(state.with_context_composer(composer))
```

### 4. Template Layer: Global Config Rendering

**File:** `prompts/core/context/global.j2`

```jinja2
{% if global_config %}
<GLOBAL_CONTEXT>
{% for key, value in global_config.items() %}
{% if value is mapping %}
<{{ key }}>
{% for k, v in value.items() %}
  <{{ k }}>{{ v }}</{{ k }}>
{% endfor %}
</{{ key }}>
{% else %}
<{{ key }}>{{ value }}</{{ key }}>
{% endif %}
{% endfor %}
</GLOBAL_CONTEXT>
{% endif %}
```

### 5. Bootstrap Layer: Wiring It All Together

**File:** `bootstrap/application.py`

```python
# Create global config provider with current date context
global_config = GlobalConfigProvider()
global_config.set_current_date()  # Uses today's date

orchestrator = AgentOrchestrator(
    llm_port=infrastructure.llm_adapter,
    worker_port=infrastructure.worker_tool,
    prompt_builder=prompt_builder,
    child_factory=child_factory,
    global_config_provider=global_config,  # Injected here
)
```

## Data Flow

1. **Bootstrap** creates `GlobalConfigProvider` and calls `set_current_date()`
2. **GlobalConfigProvider** is passed to `AgentOrchestrator` → `PipelineFactory`
3. **PipelineFactory** includes `InjectGlobalConfig` step in all pipelines
4. When a pipeline executes:
   - `InjectGlobalConfig` creates/gets `ContextComposer` from `PipelineState`
   - Calls `provider.to_global_config()` to create immutable `GlobalConfig`
   - Adds `GlobalConfig` to the `ContextComposer`
5. **Prompt building steps** detect `context_composer` and use `build_*_prompt_with_context()` methods
6. **TemplateChain.with_context()** renders `core/context/global.j2` with the global config data
7. **Resulting prompt** includes:
   ```xml
   <GLOBAL_CONTEXT>
   <current_date>2025-12-31</current_date>
   </GLOBAL_CONTEXT>
   ```

## DDD Layer Responsibility

| Layer | Component | Responsibility |
|-------|-----------|----------------|
| **Domain** | `GlobalConfig` | Immutable value object representing global context data |
| **Application** | `GlobalConfigProvider` | Service that manages mutable global config state |
| **Application** | `InjectGlobalConfig` | Pipeline step that converts provider to domain type |
| **Application** | `ContextComposer` | Builder for composing context from multiple sources |
| **Infrastructure** | `global.j2` template | Renders global config to prompt text |
| **Bootstrap** | `application.py` | Wires dependencies and initializes provider |

## Usage Example

To add more global configuration:

```python
# In bootstrap/application.py or wherever you configure the provider
global_config = GlobalConfigProvider()
global_config.set_current_date()
global_config.set("execution_id", str(uuid4()))
global_config.set("target", {"host": "192.168.1.100", "ports": [80, 443]})
```

This will render in all prompts as:
```xml
<GLOBAL_CONTEXT>
<current_date>2025-12-31</current_date>
<execution_id>abc-123-def-456</execution_id>
<target>
  <host>192.168.1.100</host>
  <ports>[80, 443]</ports>
</target>
</GLOBAL_CONTEXT>
```

## Design Decisions

1. **Immutable GlobalConfig**: The domain layer `GlobalConfig` is immutable (frozen). Mutability is in the application service (`GlobalConfigProvider`).

2. **Optional Integration**: The `global_config_provider` parameter is optional throughout the chain. If not provided, no global config is injected (backward compatible).

3. **Pipeline Step Pattern**: Using a dedicated pipeline step (`InjectGlobalConfig`) follows the existing pipeline architecture and keeps concerns separated.

4. **Template-Driven Rendering**: Using a Jinja2 template for rendering keeps the prompt format flexible and maintainable.
