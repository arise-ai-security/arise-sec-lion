# Example: Adding Bidirectional Data Between Agents

## Problem Statement

How do I add new data to be passed between:
- **Parent → Child**: Strategic guidance, constraints, priorities
- **Child → Parent**: Structured feedback, blockers, recommendations
- **Sibling ↔ Sibling**: Coordination data, claimed targets, shared discoveries

## Solution Overview

This example demonstrates how to add NEW context data types for each relationship direction. The solution introduces three new context type families:

1. **`ParentGuidance`** - Parent provides strategic direction to children
2. **`ChildFeedback`/`ChildFeedbackCollection`** - Children report structured feedback to parent
3. **`SiblingCoordination`/`SiblingCoordinationCollection`** - Siblings share coordination data

## Architecture

```
                           PARENT (Manager/Boss)
                                    │
                   ┌────────────────┼────────────────┐
                   │                │                │
          ┌────────▼────────┐      │       ┌────────▼────────┐
          │ ParentGuidance  │──────┼──────▶│  Task + Context  │
          │ (strategy, etc) │      │       │   for children   │
          └─────────────────┘      │       └──────────────────┘
                                   │
                   ┌───────────────┴───────────────┐
                   │                               │
          ┌────────▼────────┐             ┌────────▼────────┐
          │   CHILD 1       │◄───────────▶│   CHILD 2       │
          │   (Worker)      │ Sibling     │   (Worker)      │
          │                 │ Coordination│                 │
          └────────┬────────┘             └────────┬────────┘
                   │                               │
                   │        ChildFeedback          │
                   └───────────────┬───────────────┘
                                   │
                                   ▼
                   ┌───────────────────────────────┐
                   │   PARENT aggregates feedback  │
                   │   ChildFeedbackCollection     │
                   └───────────────────────────────┘
```

## Code Changes

### 1. Domain Layer: New Context Types

**File:** `core/domain/values/context/data_types.py`

#### Parent → Child: ParentGuidance

```python
class ParentGuidance(BaseModel):
    """Strategic guidance from parent to child."""
    model_config = {"frozen": True}

    strategy: str = ""
    priority_targets: tuple[str, ...] = ()
    constraints: dict[str, Any] = {}
    notes: str = ""

    @property
    def template_key(self) -> str:
        return "parent_guidance"
```

#### Child → Parent: ChildFeedback

```python
class ChildFeedback(BaseModel):
    """Feedback from child to parent."""
    model_config = {"frozen": True}

    child_id: str = ""
    success_level: str = "unknown"  # "full", "partial", "failed", "unknown"
    blockers: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    confidence_score: float = 0.0
    metadata: dict[str, Any] = {}


class ChildFeedbackCollection(BaseModel):
    """Collection of feedback from multiple children."""
    feedbacks: tuple[ChildFeedback, ...] = ()

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "feedbacks": [...],
            "success_count": sum(...),
            "partial_count": sum(...),
            "failed_count": sum(...),
        }
```

#### Sibling ↔ Sibling: SiblingCoordination

```python
class SiblingCoordination(BaseModel):
    """Coordination data shared between siblings."""
    model_config = {"frozen": True}

    sibling_id: str = ""
    claimed_targets: tuple[str, ...] = ()
    discovered_info: dict[str, Any] = {}
    warnings: tuple[str, ...] = ()
    available_for_help: bool = False


class SiblingCoordinationCollection(BaseModel):
    """Collection of coordination data from all siblings."""
    coordinations: tuple[SiblingCoordination, ...] = ()

    def to_template_dict(self) -> dict[str, Any]:
        # Merges all claimed_targets, discovered_info, warnings
        return {
            "all_claimed_targets": [...],
            "all_discovered_info": {...},
            "all_warnings": [...],
        }
```

### 2. Template Layer

**File:** `prompts/core/context/bidirectional.j2`

```jinja2
{# Parent → Child #}
{% if parent_guidance %}
<PARENT_GUIDANCE>
<strategy>{{ parent_guidance.strategy }}</strategy>
<priority_targets>{{ parent_guidance.priority_targets | join(", ") }}</priority_targets>
</PARENT_GUIDANCE>
{% endif %}

{# Child → Parent #}
{% if child_feedbacks %}
<CHILD_FEEDBACKS total="{{ child_feedbacks.total_count }}" success="{{ child_feedbacks.success_count }}">
{% for fb in child_feedbacks.feedbacks %}
<feedback child="{{ fb.child_id }}" success_level="{{ fb.success_level }}">
  <blockers>{{ fb.blockers | join(", ") }}</blockers>
  <recommendations>{{ fb.recommendations | join(", ") }}</recommendations>
</feedback>
{% endfor %}
</CHILD_FEEDBACKS>
{% endif %}

{# Sibling ↔ Sibling #}
{% if sibling_coordinations %}
<SIBLING_COORDINATION>
<already_claimed>{{ sibling_coordinations.all_claimed_targets | join(", ") }}</already_claimed>
<shared_discoveries>...</shared_discoveries>
</SIBLING_COORDINATION>
{% endif %}
```

## How to Add Each Relationship Type

### Pattern 1: Parent → Child (ParentGuidance)

**When parent spawns children:**

```python
from core.domain.values.context import ParentGuidance

# 1. Parent creates guidance
guidance = ParentGuidance(
    strategy="defensive",
    priority_targets=("authentication", "session_management"),
    constraints={"max_time_per_target": 300},
    notes="Focus on OWASP Top 10",
)

# 2. Store in SpawnPayload or SharedExecutionContext
# (extend SpawnPayload to include guidance field, or store as artifact)

# 3. Child extracts and adds to ContextComposer
context = ContextComposer()
context.add(guidance)  # Renders as parent_guidance in template
```

### Pattern 2: Child → Parent (ChildFeedback)

**When child completes and parent aggregates:**

```python
from core.domain.values.context import ChildFeedback, ChildFeedbackCollection

# 1. Child creates feedback on completion
feedback = ChildFeedback(
    child_id=str(agent.agent_id),
    success_level="partial",
    blockers=("firewall_detected",),
    recommendations=("try_alternative_port",),
    confidence_score=0.7,
)

# 2. Store as artifact in SharedExecutionContext
context.store_artifact(
    key=f"child_feedback:{agent.agent_id}",
    content_type="application/json",
    stored_by=agent.agent_id,
    content=feedback.model_dump_json(),
)

# 3. Parent retrieves all feedbacks
feedbacks = []
for key in shared_ctx.list_artifacts():
    if key.startswith("child_feedback:"):
        data = json.loads(shared_ctx.get_artifact(key).content)
        feedbacks.append(ChildFeedback(**data))

collection = ChildFeedbackCollection(feedbacks=tuple(feedbacks))
context.add(collection)  # Renders as child_feedbacks in template
```

### Pattern 3: Sibling ↔ Sibling (SiblingCoordination)

**When siblings coordinate during execution:**

```python
from core.domain.values.context import SiblingCoordination, SiblingCoordinationCollection

# 1. Sibling claims targets and shares discoveries
coordination = SiblingCoordination(
    sibling_id=str(agent.agent_id),
    claimed_targets=("port_80", "port_443"),
    discovered_info={"admin_panel": "/admin"},
    warnings=("rate_limit_approaching",),
)

# 2. Store as artifact in SharedExecutionContext
context.store_artifact(
    key=f"sibling_coord:{agent.agent_id}",
    content_type="application/json",
    stored_by=agent.agent_id,
    content=coordination.model_dump_json(),
)

# 3. Other sibling retrieves all coordinations
coordinations = []
for key in shared_ctx.list_artifacts():
    if key.startswith("sibling_coord:"):
        data = json.loads(shared_ctx.get_artifact(key).content)
        coordinations.append(SiblingCoordination(**data))

collection = SiblingCoordinationCollection(coordinations=tuple(coordinations))
context.add(collection)  # Renders as sibling_coordinations in template
```

## DDD Layer Responsibility

| Layer | Component | Responsibility |
|-------|-----------|----------------|
| **Domain** | `ParentGuidance` | Immutable value object for parent→child guidance |
| **Domain** | `ChildFeedback`, `ChildFeedbackCollection` | Immutable value objects for child→parent feedback |
| **Domain** | `SiblingCoordination`, `SiblingCoordinationCollection` | Immutable value objects for sibling↔sibling coordination |
| **Application** | Store/retrieve via `SharedExecutionContext` | Artifact storage for persistence |
| **Application** | Factory functions | Build collections from stored artifacts |
| **Infrastructure** | `bidirectional.j2` template | Renders to prompt text |

## Key Design Patterns

1. **Single Types for One Direction**: `ParentGuidance` is used once per child (parent→child)

2. **Collection Types for Many Sources**: `ChildFeedbackCollection` and `SiblingCoordinationCollection` aggregate multiple items

3. **SharedExecutionContext Storage**: Use artifacts for persistence, enabling async retrieval

4. **Keyed Artifacts**: Use predictable key patterns (`child_feedback:{id}`, `sibling_coord:{id}`) for filtering

5. **Template Aggregation**: Collections automatically merge data (all claimed targets, all discoveries)

## Adding Your Own Data Type

To add a new bidirectional data type:

1. **Define the value object** in `core/domain/values/context/data_types.py`:
   - Inherit from `BaseModel` with `frozen=True`
   - Implement `template_key` property
   - Implement `to_template_dict()` method

2. **Export it** in `core/domain/values/context/__init__.py`

3. **Create or update template** in `prompts/core/context/`

4. **Update TemplateChain** in `core/application/services/prompt_builder.py`

5. **Add factory functions** in `core/application/services/context_factories.py` (optional but recommended)

6. **Integrate storage/retrieval** using SharedExecutionContext artifacts
