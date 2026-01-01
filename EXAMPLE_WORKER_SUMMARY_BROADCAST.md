# Example: Worker Summary Broadcast to Parent AND Boss

## Problem Statement

Workers want to send their "work summary" text after completion to BOTH their immediate parent AND the root BOSS node. This enables higher-level nodes to have visibility into all worker outputs without relying on intermediate managers to relay information.

## Solution Overview

This example demonstrates how to use the **Context Passing Mechanism** to broadcast worker results to multiple ancestors. The solution introduces:

1. **`WorkerSummaryBroadcast`** - A context data type for multi-recipient summaries
2. **`SummaryBroadcastService`** - A service that stores broadcasts in SharedExecutionContext
3. **Factory functions** - For retrieving summaries by recipient type
4. **Template rendering** - Jinja2 template for displaying summaries in prompts

## Architecture

```
                    ┌─────────────────────────────────────────────────────────┐
                    │               Worker Completes Task                      │
                    └─────────────────────┬───────────────────────────────────┘
                                          │
                                          ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │          broadcast_summary_to_parent_and_boss()          │
                    │              (convenience function)                      │
                    └─────────────────────┬───────────────────────────────────┘
                                          │
                                          ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │              SummaryBroadcastService                     │
                    │   Stores artifacts with recipient-keyed prefixes:        │
                    │   - worker_summary_broadcast:parent:{worker_id}          │
                    │   - worker_summary_broadcast:boss:{worker_id}            │
                    └─────────────────────┬───────────────────────────────────┘
                                          │
                                          ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │              SharedExecutionContext                      │
                    │         (Artifacts stored for each recipient)            │
                    └─────────────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────┴───────────────────┐
                    ▼                                         ▼
    ┌───────────────────────────────┐       ┌───────────────────────────────┐
    │  worker_summaries_for_parent  │       │   worker_summaries_for_boss   │
    │       (factory function)       │       │      (factory function)       │
    └───────────────────────────────┘       └───────────────────────────────┘
                    │                                         │
                    ▼                                         ▼
    ┌───────────────────────────────┐       ┌───────────────────────────────┐
    │      Parent receives all      │       │     Boss receives all         │
    │    summaries tagged "parent"   │       │   summaries tagged "boss"     │
    └───────────────────────────────┘       └───────────────────────────────┘
```

## Code Changes

### 1. Domain Layer: WorkerSummaryBroadcast Data Type

**File:** `core/domain/values/context/data_types.py`

```python
class WorkerSummaryEntry(BaseModel):
    """Single worker summary broadcast entry."""
    model_config = {"frozen": True}

    worker_id: str
    worker_task: str
    summary_text: str
    timestamp: str = ""


class WorkerSummaryBroadcast(BaseModel):
    """Worker summaries broadcast to specific recipients.

    The recipient_type determines who receives the broadcast:
    - "parent": Only immediate parent receives
    - "boss": Only root boss receives
    - "both": Both parent AND boss receive (default)
    - "depth_N": Ancestor at specific depth (e.g., "depth_1")
    """
    model_config = {"frozen": True}

    recipient_type: str = "both"
    summaries: tuple[WorkerSummaryEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "worker_summaries"
```

### 2. Application Layer: SummaryBroadcastService

**File:** `core/application/services/summary_broadcast_service.py`

```python
class SummaryBroadcastService:
    """Service for broadcasting and retrieving worker summaries."""

    def broadcast(
        self,
        worker_id: UUID,
        worker_task: str,
        summary_text: str,
        recipients: list[str],  # ["parent", "boss", "both"]
        context: SharedExecutionContext,
        parent_id: UUID | None = None,
    ) -> None:
        """Broadcast a worker summary to specified recipients.

        Stores the summary in SharedExecutionContext as an artifact
        keyed by recipient type.
        """
        # Expand "both" to ["parent", "boss"]
        # Store artifact for each recipient with unique key
        artifact_key = f"worker_summary_broadcast:{recipient}:{worker_id}"
        context.store_artifact(key=artifact_key, ...)

    def get_summaries_for_recipient(
        self,
        recipient_type: str,
        context: SharedExecutionContext,
    ) -> WorkerSummaryBroadcast:
        """Retrieve all summaries targeted at a recipient."""
        # Scan artifacts with matching prefix
        prefix = f"worker_summary_broadcast:{recipient_type}:"
        # Build WorkerSummaryBroadcast from matching artifacts


def broadcast_summary_to_parent_and_boss(
    worker_id: UUID,
    worker_task: str,
    summary_text: str,
    context: SharedExecutionContext,
    parent_id: UUID | None = None,
) -> None:
    """Convenience function to broadcast to both parent and boss."""
```

### 3. Factory Functions for Retrieving Summaries

**File:** `core/application/services/context_factories.py`

```python
def worker_summaries_for_parent(
    context: SharedExecutionContext,
) -> WorkerSummaryBroadcast:
    """Get worker summaries broadcast to parent recipients."""
    service = SummaryBroadcastService()
    return service.get_summaries_for_recipient(
        recipient_type="parent",
        context=context,
    )


def worker_summaries_for_boss(
    context: SharedExecutionContext,
) -> WorkerSummaryBroadcast:
    """Get worker summaries broadcast to boss recipients."""
    service = SummaryBroadcastService()
    return service.get_summaries_for_recipient(
        recipient_type="boss",
        context=context,
    )
```

### 4. Template Layer: Summary Rendering

**File:** `prompts/core/context/summaries.j2`

```jinja2
{% if worker_summaries and worker_summaries.summaries %}
<WORKER_SUMMARIES recipient="{{ worker_summaries.recipient_type }}" count="{{ worker_summaries.total_count }}">
{% for summary in worker_summaries.summaries %}
<summary worker="{{ summary.worker_id }}"{% if summary.timestamp %} at="{{ summary.timestamp }}"{% endif %}>
<task>{{ summary.worker_task }}</task>
<content>{{ summary.summary_text }}</content>
</summary>
{% endfor %}
</WORKER_SUMMARIES>
{% endif %}
```

### 5. Integration: Automatic Broadcast on Worker Completion

**File:** `core/application/execution_service.py`

```python
async def _process_worker_context_updates(self, agent: AgentSession) -> None:
    """Extract and store context updates from worker result.

    Also broadcasts the worker's summary to both parent and boss.
    """
    # ... get SharedExecutionContext ...

    # Broadcast worker summary to parent AND boss
    broadcast_summary_to_parent_and_boss(
        worker_id=agent.agent_id,
        worker_task=agent.task_description,
        summary_text=agent.result,
        context=context,
        parent_id=agent.parent_id,
    )

    # ... rest of processing ...
```

## Data Flow

1. **Worker completes** its task with a result
2. **ExecutionService** calls `_process_worker_context_updates()`
3. **`broadcast_summary_to_parent_and_boss()`** is invoked
4. **SummaryBroadcastService** creates artifacts in SharedExecutionContext:
   - `worker_summary_broadcast:parent:{worker_id}` → for parent
   - `worker_summary_broadcast:boss:{worker_id}` → for boss
5. **When parent builds prompt**, it can use:
   ```python
   context.add(worker_summaries_for_parent(shared_context))
   ```
6. **When boss builds prompt**, it can use:
   ```python
   context.add(worker_summaries_for_boss(shared_context))
   ```
7. **TemplateChain.with_context()** renders `summaries.j2`
8. **Resulting prompt** includes:
   ```xml
   <WORKER_SUMMARIES recipient="parent" count="3">
   <summary worker="worker-123" at="2025-12-31T10:30:00">
   <task>Scan authentication module</task>
   <content>Found SQL injection vulnerability in login handler...</content>
   </summary>
   ...
   </WORKER_SUMMARIES>
   ```

## DDD Layer Responsibility

| Layer | Component | Responsibility |
|-------|-----------|----------------|
| **Domain** | `WorkerSummaryBroadcast`, `WorkerSummaryEntry` | Immutable value objects for broadcast data |
| **Application** | `SummaryBroadcastService` | Stores/retrieves broadcasts via SharedContext |
| **Application** | `broadcast_summary_to_parent_and_boss()` | Convenience function for common use case |
| **Application** | `worker_summaries_for_*()` | Factory functions for retrieving summaries |
| **Application** | `ExecutionService` | Triggers broadcast on worker completion |
| **Infrastructure** | `summaries.j2` template | Renders summaries to prompt text |

## Usage Example

### Worker Broadcasting (Automatic)

When workers complete, their summary is automatically broadcast:

```python
# This happens automatically in ExecutionService._process_worker_context_updates()
# No manual intervention needed
```

### Parent Receiving Summaries

```python
from core.application.services.context_factories import worker_summaries_for_parent

# In a Pipeline step building manager prompt:
shared_ctx = await shared_context_port.get(root_id)
context = ContextComposer()
context.add(worker_summaries_for_parent(shared_ctx))
# Now the parent's prompt includes all worker summaries targeted at "parent"
```

### Boss Receiving Summaries

```python
from core.application.services.context_factories import worker_summaries_for_boss

# In a Pipeline step building boss prompt:
shared_ctx = await shared_context_port.get(root_id)
context = ContextComposer()
context.add(worker_summaries_for_boss(shared_ctx))
# Now the boss's prompt includes all worker summaries targeted at "boss"
```

## Design Decisions

1. **Artifact-Based Storage**: Using SharedExecutionContext artifacts for storage leverages existing event-sourcing infrastructure with OCC.

2. **Recipient Keying**: Artifacts are keyed by `{prefix}:{recipient}:{worker_id}` enabling efficient filtering by recipient type.

3. **Automatic Broadcasting**: Integration in `_process_worker_context_updates()` ensures all worker results are automatically broadcast without requiring explicit calls.

4. **Dual Targeting with "both"**: The `recipients=["both"]` expands to both "parent" and "boss", storing two artifacts per broadcast.

5. **Factory Functions**: Provide clean API for retrieving summaries targeted at specific recipients.
