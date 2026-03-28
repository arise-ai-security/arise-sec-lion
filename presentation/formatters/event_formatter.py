"""Event formatters using Strategy pattern (OCP compliant).

Each event type has its own formatter class, making it easy to add new
event types without modifying existing code.
"""

from abc import ABC, abstractmethod


class EventFormatterStrategy(ABC):
    """Abstract base for event formatting strategies."""

    @abstractmethod
    def format(self, event: object) -> str | None:
        """Format event to display string. Return None to skip display."""


class AgentCreatedFormatter(EventFormatterStrategy):
    """Format AgentCreated events."""

    def format(self, event: object) -> str:
        agent_id = self._short_id(event)
        role = getattr(event, "role", "?")
        parent_id = getattr(event, "parent_id", None)
        parent_info = f" (parent: {str(parent_id)[:8]})" if parent_id else " (root)"
        return f"   🤖 Agent {agent_id}... created as {role}{parent_info}"

    def _short_id(self, event: object) -> str:
        return str(getattr(event, "aggregate_id", "?"))[:8]


class TaskAssignedFormatter(EventFormatterStrategy):
    """Format TaskAssigned events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        task = getattr(event, "task_description", "")
        task_preview = task[:60] + "..." if len(task) > 60 else task
        return f"   📋 [{agent_id}] Task: {task_preview}"


class StatusChangedFormatter(EventFormatterStrategy):
    """Format StatusChanged events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        old = getattr(event, "old_status", "?")
        new = getattr(event, "new_status", "?")
        reason = getattr(event, "reason", "")
        reason_info = f" ({reason})" if reason else ""
        return f"   → [{agent_id}] {old} → {new}{reason_info}"


class ComplexityEvaluatedFormatter(EventFormatterStrategy):
    """Format ComplexityEvaluated events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        complexity = getattr(event, "complexity", "?")
        role = getattr(event, "determined_role", "?")
        emoji = "🔧" if complexity == "simple" else "🔀"
        return f"   {emoji} [{agent_id}] Complexity: {complexity} → becomes {role.upper()}"


class SubtasksDefinedFormatter(EventFormatterStrategy):
    """Format SubtasksDefined events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        subtasks = getattr(event, "subtasks", [])
        return f"   📑 [{agent_id}] Decomposed into {len(subtasks)} subtask(s)"


class ChildSpawnedFormatter(EventFormatterStrategy):
    """Format ChildSpawned events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        child_id = str(getattr(event, "child_id", "?"))[:8]
        child_role = getattr(event, "child_role", "?")
        subtask = getattr(event, "subtask", None)
        desc = getattr(subtask, "description", "")[:40] if subtask else ""

        lines = [f"   👶 [{agent_id}] Spawned child {child_id}... as {child_role}"]
        if desc:
            lines.append(f"      └─ {desc}...")
        return "\n".join(lines)


class CodeGenerationStartedFormatter(EventFormatterStrategy):
    """Format CodeGenerationStarted events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        tool = getattr(event, "tool_name", "?")
        return f"   ⚡ [{agent_id}] Code generation started (tool: {tool})"


class ThoughtCapturedFormatter(EventFormatterStrategy):
    """Format ThoughtCaptured events (silent)."""

    def format(self, event: object) -> str | None:
        return None  # Silent - don't display


class WorkCompletedFormatter(EventFormatterStrategy):
    """Format WorkCompleted events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        return f"   ✅ [{agent_id}] Work completed"


class WorkFailedFormatter(EventFormatterStrategy):
    """Format WorkFailed events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        reason = getattr(event, "reason", "unknown")[:80]
        return f"   ❌ [{agent_id}] Work failed: {reason}"


class ChildCompletedFormatter(EventFormatterStrategy):
    """Format ChildCompleted events."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        child_id = str(getattr(event, "child_id", "?"))[:8]
        return f"   ✓ [{agent_id}] Child {child_id}... completed"


class DefaultFormatter(EventFormatterStrategy):
    """Default formatter for unknown event types."""

    def format(self, event: object) -> str:
        agent_id = str(getattr(event, "aggregate_id", "?"))[:8]
        event_type = type(event).__name__
        return f"   • [{agent_id}] {event_type}"


class EventFormatter:
    """Registry-based event formatter (Strategy pattern).

    New event types can be registered without modifying existing code.
    Follows Open/Closed Principle.
    """

    _registry: dict[str, EventFormatterStrategy] = {}
    _default: EventFormatterStrategy = DefaultFormatter()

    @classmethod
    def register(cls, event_type: str, formatter: EventFormatterStrategy) -> None:
        """Register a formatter for an event type."""
        cls._registry[event_type] = formatter

    @classmethod
    def format(cls, event: object) -> str | None:
        """Format an event using registered formatter."""
        event_type = type(event).__name__
        formatter = cls._registry.get(event_type, cls._default)
        return formatter.format(event)


# Register all formatters
EventFormatter.register("AgentCreated", AgentCreatedFormatter())
EventFormatter.register("TaskAssigned", TaskAssignedFormatter())
EventFormatter.register("StatusChanged", StatusChangedFormatter())
EventFormatter.register("ComplexityEvaluated", ComplexityEvaluatedFormatter())
EventFormatter.register("SubtasksDefined", SubtasksDefinedFormatter())
EventFormatter.register("ChildSpawned", ChildSpawnedFormatter())
EventFormatter.register("CodeGenerationStarted", CodeGenerationStartedFormatter())
EventFormatter.register("ThoughtCaptured", ThoughtCapturedFormatter())
EventFormatter.register("WorkCompleted", WorkCompletedFormatter())
EventFormatter.register("WorkFailed", WorkFailedFormatter())
EventFormatter.register("ChildCompleted", ChildCompletedFormatter())


class ProgressDisplayFormatter:
    """Display formatter for real-time progress (callback signature compatible)."""

    @staticmethod
    def display(event: object, _agent: object) -> None:
        """Print formatted event to stdout. Compatible with progress callback."""
        output = EventFormatter.format(event)
        if output is not None:
            print(output)
