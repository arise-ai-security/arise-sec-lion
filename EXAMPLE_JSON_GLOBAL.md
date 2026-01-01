# Example: JSON Global Context

## Problem Statement

How do I add new data in JSON format to be passed to global context and make every node render this data to prompt?

## Solution Overview

This example introduces `JsonGlobalData` and `JsonGlobalDataCollection` types that allow arbitrary JSON-structured data to be passed through the agent hierarchy and rendered into prompts for all nodes.

## Architecture

```
                    JsonGlobalData
                          │
                          ▼
           ┌──────────────────────────────┐
           │      ContextComposer         │
           │  .add(JsonGlobalData(...))   │
           └──────────────────────────────┘
                          │
                          ▼
           ┌──────────────────────────────┐
           │   TemplateChain.with_context │
           │  renders json_global.j2      │
           └──────────────────────────────┘
                          │
                          ▼
           ┌──────────────────────────────┐
           │   All Agents See JSON Data   │
           │   in Structured XML Format   │
           └──────────────────────────────┘
```

## Code Components

### 1. Domain Layer: JsonGlobalData Types

**File:** `core/domain/values/context/data_types.py`

```python
class JsonGlobalData(BaseModel):
    """JSON-formatted global data available to all agents.

    Use for structured configuration, target information, or any JSON
    data that every node in the hierarchy should have access to.
    """

    model_config = {"frozen": True}

    label: str  # Identifier for this data (e.g., "target_system", "scan_config")
    data: dict[str, Any]  # The JSON data
    schema_hint: str = ""  # Optional description of the data structure

    @property
    def template_key(self) -> str:
        return f"json_global_{self.label}"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "schema_hint": self.schema_hint,
            "data": self.data,
        }


class JsonGlobalDataCollection(BaseModel):
    """Collection of multiple JSON global data blocks."""

    model_config = {"frozen": True}

    items: tuple[JsonGlobalData, ...] = ()

    @property
    def template_key(self) -> str:
        return "json_global_collection"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_template_dict() for item in self.items],
            "total_count": len(self.items),
            "labels": [item.label for item in self.items],
        }
```

### 2. Template Layer

**File:** `prompts/core/context/json_global.j2`

```jinja2
{% if json_global_collection %}
<JSON_GLOBAL_DATA count="{{ json_global_collection.total_count }}">
{% for item in json_global_collection.items %}
<JSON_DATA label="{{ item.label }}"{% if item.schema_hint %} hint="{{ item.schema_hint }}"{% endif %}>
{% for key, value in item.data.items() %}
<{{ key }}>{{ value | tojson }}</{{ key }}>
{% endfor %}
</JSON_DATA>
{% endfor %}
</JSON_GLOBAL_DATA>
{% endif %}
```

### 3. TemplateChain Integration

**File:** `core/application/services/prompt_builder.py`

```python
def with_context(self, context: "ContextComposer") -> Self:
    # ... existing context rendering ...

    # Render JSON global context
    has_json_global = context.has("json_global_collection") or context.has_any(
        *(k for k in context.keys() if k.startswith("json_global_"))
    )
    if has_json_global:
        self.render_optional("core/context/json_global.j2", _context=ctx_dict, **ctx_dict)

    return self
```

## Usage Patterns

### Pattern 1: Single JSON Data Block

```python
from core.domain.values.context import JsonGlobalData
from core.application.services.context_composer import ContextComposer

# Define structured JSON data
target_config = JsonGlobalData(
    label="target_system",
    data={
        "host": "192.168.1.100",
        "ports": [80, 443, 8080],
        "services": {
            "web": {"port": 80, "technology": "nginx"},
            "api": {"port": 8080, "version": "v2"},
        },
        "credentials": None,
    },
    schema_hint="Target system configuration for penetration testing",
)

# Add to context
context = ContextComposer().add(target_config)

# Build prompt
prompt = builder.build_worker_prompt_with_context(
    task_description="Scan the target system",
    context=context,
)
```

**Rendered Output:**
```xml
<JSON_DATA label="target_system" hint="Target system configuration for penetration testing">
<host>192.168.1.100</host>
<ports>[80, 443, 8080]</ports>
<services>
  <web>{"port": 80, "technology": "nginx"}</web>
  <api>{"port": 8080, "version": "v2"}</api>
</services>
<credentials>null</credentials>
</JSON_DATA>
```

### Pattern 2: Multiple JSON Data Blocks via Collection

```python
from core.domain.values.context import JsonGlobalData, JsonGlobalDataCollection

# Create multiple JSON data blocks
target_data = JsonGlobalData(
    label="target",
    data={"host": "example.com", "ip": "93.184.216.34"},
)

scan_config = JsonGlobalData(
    label="scan_config",
    data={
        "mode": "stealth",
        "timeout": 30,
        "retry_count": 3,
    },
    schema_hint="Scanner configuration settings",
)

credentials = JsonGlobalData(
    label="credentials",
    data={
        "username": "admin",
        "auth_type": "basic",
    },
)

# Bundle into collection
collection = JsonGlobalDataCollection(
    items=(target_data, scan_config, credentials)
)

# Add to context
context = ContextComposer().add(collection)
```

**Rendered Output:**
```xml
<JSON_GLOBAL_DATA count="3">
<JSON_DATA label="target">
<host>example.com</host>
<ip>93.184.216.34</ip>
</JSON_DATA>
<JSON_DATA label="scan_config" hint="Scanner configuration settings">
<mode>stealth</mode>
<timeout>30</timeout>
<retry_count>3</retry_count>
</JSON_DATA>
<JSON_DATA label="credentials">
<username>admin</username>
<auth_type>basic</auth_type>
</JSON_DATA>
</JSON_GLOBAL_DATA>
```

### Pattern 3: Global Injection via Pipeline Step

```python
from core.application.pipeline.base import PipelineStep
from core.application.pipeline.context import PipelineState
from core.domain.values.context import JsonGlobalData

class InjectJsonGlobalConfig(PipelineStep):
    """Inject JSON configuration into all agent contexts."""

    def __init__(self, config_data: dict[str, Any]) -> None:
        self._config = JsonGlobalData(
            label="global_config",
            data=config_data,
            schema_hint="System-wide configuration",
        )

    async def execute(self, state: PipelineState) -> PipelineState:
        if state.context_composer:
            state.context_composer.add(self._config)
        return state
```

## Design Decisions

1. **Template Key Prefixing**: Individual `JsonGlobalData` uses `json_global_{label}` as template key, allowing multiple distinct JSON blocks with different labels.

2. **Collection Support**: `JsonGlobalDataCollection` bundles multiple JSON blocks for batch rendering, with automatic count and label tracking.

3. **Structured XML Output**: JSON data is rendered as XML with nested structure, making it easy for LLMs to parse and reference.

4. **Schema Hints**: Optional `schema_hint` provides context about the data structure to help LLMs understand the data.

5. **Immutability**: Both types use `frozen=True` for thread-safety and predictable behavior.

## DDD Layer Responsibility

| Layer | Component | Responsibility |
|-------|-----------|----------------|
| **Domain** | `JsonGlobalData` | Immutable value object for single JSON block |
| **Domain** | `JsonGlobalDataCollection` | Immutable collection of JSON blocks |
| **Application** | `ContextComposer.add()` | Compose JSON data into context |
| **Application** | `TemplateChain.with_context()` | Detect and render JSON context |
| **Infrastructure** | `json_global.j2` | Template for XML rendering |

## Files Modified

- `core/domain/values/context/data_types.py` - Added `JsonGlobalData`, `JsonGlobalDataCollection`
- `core/domain/values/context/__init__.py` - Exported new types
- `core/application/services/prompt_builder.py` - Updated `with_context()` for JSON rendering
- `prompts/core/context/json_global.j2` - Template for JSON data rendering
