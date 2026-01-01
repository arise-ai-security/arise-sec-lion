# Example: Worker Summary to Depth-1 Ancestor

## Problem Statement

Workers want to send their "work summary" text after completion to the ancestor that is the first child of the BOSS node (i.e., the ancestor at depth=1). This enables a coordinator pattern where a specific manager-level node aggregates all worker outputs.

## Solution Overview

This example demonstrates how to use the **Context Passing Mechanism** to target summaries at specific hierarchy depths. The solution introduces:

1. **`DepthTargetedSummary`** - A context data type for depth-based targeting
2. **`DepthBroadcastService`** - A service for depth-targeted broadcasts
3. **Factory functions** - For retrieving summaries by target depth
4. **Template rendering** - Jinja2 template for displaying depth-targeted summaries

## Architecture

```
              Hierarchy Depth Reference
              ═════════════════════════
              depth=0: BOSS (root)
              depth=1: First child of BOSS ← Coordinator/Aggregator
              depth=2: Children of depth-1
              depth=N: Workers at various depths

              ┌─────────────────────────────────────────────────────────┐
              │            Worker at depth=N completes                   │
              └─────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
              ┌─────────────────────────────────────────────────────────┐
              │         broadcast_summary_to_depth1(...)                │
              │          (targets depth=1 ancestor)                     │
              └─────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
              ┌─────────────────────────────────────────────────────────┐
              │              DepthBroadcastService                      │
              │   Stores artifact with depth-keyed prefix:              │
              │   depth_targeted_summary:depth_1:{worker_id}            │
              └─────────────────────┬───────────────────────────────────┘
                                    │
                                    ▼
              ┌─────────────────────────────────────────────────────────┐
              │              SharedExecutionContext                     │
              │         (Artifacts stored by target depth)              │
              └─────────────────────────────────────────────────────────┘
                                    │
                                    ▼
              ┌─────────────────────────────────────────────────────────┐
              │            depth1_summaries(context)                    │
              │         (retrieves all depth=1 targeted summaries)      │
              └─────────────────────────────────────────────────────────┘
                                    │
                                    ▼
              ┌─────────────────────────────────────────────────────────┐
              │           Depth-1 Ancestor receives all                 │
              │         worker summaries targeted at depth=1            │
              └─────────────────────────────────────────────────────────┘
```

## Code Changes

### 1. Domain Layer: DepthTargetedSummary Data Type

**File:** `core/domain/values/context/data_types.py`

```python
class DepthTargetedSummaryEntry(BaseModel):
    """Single depth-targeted worker summary entry."""
    model_config = {"frozen": True}

    worker_id: str
    worker_task: str
    summary_text: str
    source_depth: int  # Depth of the worker

    timestamp: str = ""


class DepthTargetedSummary(BaseModel):
    """Worker summaries targeted at a specific ancestor depth.

    Depth mapping:
    - depth=0: BOSS (root)
    - depth=1: First child of BOSS (e.g., main coordinator/manager)
    - depth=N: Ancestor at that specific depth
    """
    model_config = {"frozen": True}

    target_depth: int = 1  # Default to first child of boss
    summaries: tuple[DepthTargetedSummaryEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "depth_targeted_summaries"
```

### 2. Application Layer: DepthBroadcastService

**File:** `core/application/services/depth_broadcast_service.py`

```python
class DepthBroadcastService:
    """Service for broadcasting and retrieving depth-targeted worker summaries."""

    def broadcast(
        self,
        worker_id: UUID,
        worker_task: str,
        summary_text: str,
        target_depth: int,  # 0=boss, 1=first child, etc.
        source_depth: int,  # Worker's depth
        context: SharedExecutionContext,
    ) -> None:
        """Broadcast a worker summary to a specific ancestor depth."""
        # Key format: depth_targeted_summary:depth_N:worker_id
        artifact_key = f"depth_targeted_summary:depth_{target_depth}:{worker_id}"
        context.store_artifact(key=artifact_key, ...)

    def get_summaries_for_depth(
        self,
        target_depth: int,
        context: SharedExecutionContext,
    ) -> DepthTargetedSummary:
        """Retrieve all summaries targeted at a specific depth."""
        # Scan artifacts with matching prefix
        prefix = f"depth_targeted_summary:depth_{target_depth}:"
        # Build DepthTargetedSummary from matching artifacts


def broadcast_summary_to_depth1(
    worker_id: UUID,
    worker_task: str,
    summary_text: str,
    source_depth: int,
    context: SharedExecutionContext,
) -> None:
    """Convenience function to broadcast to depth=1 ancestor."""
```

### 3. Factory Functions

**File:** `core/application/services/context_factories.py`

```python
def depth_targeted_summaries(
    target_depth: int,
    context: SharedExecutionContext,
) -> DepthTargetedSummary:
    """Get worker summaries targeted at a specific ancestor depth."""
    service = DepthBroadcastService()
    return service.get_summaries_for_depth(
        target_depth=target_depth,
        context=context,
    )


def depth1_summaries(
    context: SharedExecutionContext,
) -> DepthTargetedSummary:
    """Get worker summaries targeted at depth=1 (first child of boss)."""
    return depth_targeted_summaries(target_depth=1, context=context)
```

### 4. Template Layer

**File:** `prompts/core/context/depth_summaries.j2`

```jinja2
{% if depth_targeted_summaries and depth_targeted_summaries.summaries %}
<DEPTH_TARGETED_SUMMARIES target_depth="{{ depth_targeted_summaries.target_depth }}" count="{{ depth_targeted_summaries.total_count }}">
{% for summary in depth_targeted_summaries.summaries %}
<summary worker="{{ summary.worker_id }}" from_depth="{{ summary.source_depth }}"{% if summary.timestamp %} at="{{ summary.timestamp }}"{% endif %}>
<task>{{ summary.worker_task }}</task>
<content>{{ summary.summary_text }}</content>
</summary>
{% endfor %}
</DEPTH_TARGETED_SUMMARIES>
{% endif %}
```

## Data Flow

1. **Worker at depth N** completes its task
2. **Worker calls** `broadcast_summary_to_depth1()` (or uses DepthBroadcastService directly)
3. **DepthBroadcastService** creates artifact in SharedExecutionContext:
   - Key: `depth_targeted_summary:depth_1:{worker_id}`
   - Content: JSON with summary, source_depth, target_depth
4. **When depth-1 ancestor builds prompt**, it calls:
   ```python
   context.add(depth1_summaries(shared_context))
   ```
5. **DepthBroadcastService.get_summaries_for_depth()** scans for matching artifacts
6. **TemplateChain.with_context()** renders `depth_summaries.j2`
7. **Resulting prompt** includes:
   ```xml
   <DEPTH_TARGETED_SUMMARIES target_depth="1" count="5">
   <summary worker="worker-123" from_depth="3" at="2025-12-31T10:30:00">
   <task>Scan authentication module</task>
   <content>Found SQL injection vulnerability in login handler...</content>
   </summary>
   ...
   </DEPTH_TARGETED_SUMMARIES>
   ```

## DDD Layer Responsibility

| Layer | Component | Responsibility |
|-------|-----------|----------------|
| **Domain** | `DepthTargetedSummary`, `DepthTargetedSummaryEntry` | Immutable value objects for depth-targeted broadcasts |
| **Application** | `DepthBroadcastService` | Stores/retrieves broadcasts by target depth |
| **Application** | `broadcast_summary_to_depth1()` | Convenience function for common use case |
| **Application** | `depth1_summaries()` | Factory function for depth=1 retrieval |
| **Infrastructure** | `depth_summaries.j2` template | Renders summaries to prompt text |

## Usage Example

### Worker Broadcasting to Depth-1

```python
from core.application.services.depth_broadcast_service import broadcast_summary_to_depth1

# After worker completes (in ExecutionService or Pipeline step):
broadcast_summary_to_depth1(
    worker_id=agent.agent_id,
    worker_task=agent.task_description,
    summary_text=agent.result,
    source_depth=agent.hierarchy_limits.current_depth,  # Worker's depth
    context=shared_context,
)
```

### Depth-1 Ancestor Receiving Summaries

```python
from core.application.services.context_factories import depth1_summaries

# In Pipeline step building depth-1 ancestor's prompt:
shared_ctx = await shared_context_port.get(root_id)
context = ContextComposer()
context.add(depth1_summaries(shared_ctx))
# Now the depth-1 ancestor sees all worker summaries
```

### Custom Depth Targeting

```python
from core.application.services.depth_broadcast_service import DepthBroadcastService
from core.application.services.context_factories import depth_targeted_summaries

# Worker broadcasts to depth=2 instead of depth=1
service = DepthBroadcastService()
service.broadcast(
    worker_id=agent.agent_id,
    worker_task=agent.task_description,
    summary_text="Custom summary...",
    target_depth=2,
    source_depth=5,
    context=shared_context,
)

# Depth-2 ancestor retrieves summaries
context.add(depth_targeted_summaries(target_depth=2, context=shared_ctx))
```

## Design Decisions

1. **Depth-Based Keying**: Artifacts are keyed by `depth_{N}` enabling efficient filtering by target depth regardless of the actual ancestor's identity.

2. **Source Depth Tracking**: Each summary includes `source_depth` so recipients know how deep in the hierarchy the worker was.

3. **Coordinator Pattern**: Depth=1 is the default target because it's the natural position for a coordinator that aggregates worker outputs before reporting to BOSS.

4. **Flexible Depth Targeting**: While `broadcast_summary_to_depth1()` is provided for convenience, the underlying service supports any target depth.

5. **Hierarchy-Agnostic**: The worker doesn't need to know the actual agent ID of the target—just the depth level it wants to reach.
