# Milestone 4: Context Passing Between Nodes

**Status**: Not Started
**Prerequisites**: Milestones 1-3 complete

**Goal**: Enable bidirectional context flow (parent→child, child→parent, across hierarchy).

---

## Implementation Steps

1. Extend `ChildSpawned` with `parent_context`
2. Extend `ChildCompleted` with `artifacts`, `context_updates`
3. Update prompt templates
4. Implement `get_context_for_child()`

---

## 4.1 Enhanced ChildSpawned Event

**File**: `core/domain/events.py`

Extend `ChildSpawned`:
```python
class ChildSpawned(DomainEvent):
    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]
    # NEW fields:
    parent_context: dict[str, Any] = {}  # Context from parent to child
    execution_context: dict[str, Any] = {}  # Limits, budget, depth
```

---

## 4.2 Enhanced ChildCompleted Event

**File**: `core/domain/events.py`

Extend `ChildCompleted`:
```python
class ChildCompleted(DomainEvent):
    child_id: UUID
    result: str
    # NEW fields:
    artifacts: list[str] = []  # Files created
    decisions: list[str] = []  # Key decisions made
    context_updates: dict[str, Any] = {}  # Updates to propagate upward
```

---

## 4.3 AgentSession Context Storage

**File**: `core/domain/model.py`

Add to `_initialize_defaults()`:
```python
self.parent_context: dict[str, Any] = {}  # Context received from parent
self.local_context: dict[str, Any] = {}  # Context generated during execution
```

Add method:
```python
def get_context_for_child(self) -> dict[str, Any]:
    """Build context to pass to child (task, decisions, constraints)."""
    return {
        "parent_task": self.task_description,
        "parent_role": self.role.value,
        "depth": self._execution_context.current_depth if self._execution_context else 0,
        "ancestry": self._build_ancestry_summary(),
        **self.local_context,
    }
```

---

## 4.4 Prompt Enhancement

**File**: `core/domain/prompt_builder.py`

Modify `build_complexity_evaluation_prompt()` and `build_task_decomposition_prompt()`:
```python
def build_complexity_evaluation_prompt(
    self,
    task_description: str,
    agent_id: UUID,
    parent_context: dict[str, Any] | None = None,  # NEW
) -> str:
    # Include parent context in prompt if provided
```

**File**: `prompts/tasks/complexity_evaluation.j2`
```jinja2
{% if parent_context %}
<PARENT_CONTEXT>
Parent Task: {{ parent_context.parent_task }}
Depth in Hierarchy: {{ parent_context.depth }}
Ancestry: {{ parent_context.ancestry }}
</PARENT_CONTEXT>
{% endif %}
```

---

## 4.5 Context Propagation in Execution Service

**File**: `core/application/execution_service.py`

In `_handle_child_spawning()`:
```python
child = AgentSession.create(
    session_id=child_event.child_id,
    role=AgentRole(child_event.child_role),
    config=child_config,
    parent_id=parent.session_id,
    parent_context=child_event.parent_context,  # NEW
)
```

---

## Testing Strategy

Key test scenarios:
- Parent context passed to child via ChildSpawned event
- Child artifacts propagated to parent via ChildCompleted
- Context influences prompt generation
- Ancestry summary correctly built

## Files to Modify/Create

| File | Action |
|------|--------|
| `core/domain/events.py` | Extend ChildSpawned, ChildCompleted |
| `core/domain/model.py` | Add context storage, get_context_for_child() |
| `core/domain/prompt_builder.py` | Add parent_context parameter |
| `prompts/tasks/complexity_evaluation.j2` | Add parent context section |
| `prompts/tasks/task_decomposition.j2` | Add parent context section |
| `core/application/execution_service.py` | Pass context during child creation |
