"""Tests for event formatters using Strategy pattern.

These tests verify the EventFormatter correctly formats different
event types using the registered strategy classes.
"""



from dataclasses import dataclass
from io import StringIO
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from presentation.formatters import EventFormatter, ProgressDisplayFormatter


@dataclass
class MockEvent:
    """Mock event for testing formatters."""

    aggregate_id: Any = None

    def __post_init__(self) -> None:
        if self.aggregate_id is None:
            self.aggregate_id = uuid4()


@dataclass
class MockAgentCreated(MockEvent):
    """Mock AgentCreated event."""

    role: str = "BOSS"
    parent_id: Any = None


@dataclass
class MockTaskAssigned(MockEvent):
    """Mock TaskAssigned event."""

    task_description: str = "Test task description"


@dataclass
class MockStatusChanged(MockEvent):
    """Mock StatusChanged event."""

    old_status: str = "pending"
    new_status: str = "running"
    reason: str = ""


@dataclass
class MockComplexityEvaluated(MockEvent):
    """Mock ComplexityEvaluated event."""

    complexity: str = "simple"
    determined_role: str = "worker"


@dataclass
class MockSubtasksDefined(MockEvent):
    """Mock SubtasksDefined event."""

    subtasks: list = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.subtasks is None:
            self.subtasks = []


@dataclass
class MockSubtask:
    """Mock subtask for ChildSpawned events."""

    description: str = "Do something"


@dataclass
class MockChildSpawned(MockEvent):
    """Mock ChildSpawned event."""

    child_id: Any = None
    child_role: str = "worker"
    subtask: MockSubtask = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.child_id is None:
            self.child_id = uuid4()


@dataclass
class MockCodeGenerationStarted(MockEvent):
    """Mock CodeGenerationStarted event."""

    tool_name: str = "claude_code"


@dataclass
class MockThoughtCaptured(MockEvent):
    """Mock ThoughtCaptured event."""

    thought: str = "Thinking..."


@dataclass
class MockWorkCompleted(MockEvent):
    """Mock WorkCompleted event."""

    result: str = "Work done"


@dataclass
class MockWorkFailed(MockEvent):
    """Mock WorkFailed event."""

    reason: str = "Something went wrong"


@dataclass
class MockChildCompleted(MockEvent):
    """Mock ChildCompleted event."""

    child_id: Any = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.child_id is None:
            self.child_id = uuid4()


class TestEventFormatter:
    """Tests for EventFormatter registry-based formatting."""

    def test_format_agent_created(self) -> None:
        """Test AgentCreated event formatting."""
        event = MockAgentCreated(role="BOSS")
        # Rename class to match registry
        event.__class__.__name__ = "AgentCreated"

        result = EventFormatter.format(event)

        assert "Agent" in result
        assert "created" in result
        assert "BOSS" in result
        assert "(root)" in result

    def test_format_agent_created_with_parent(self) -> None:
        """Test AgentCreated event with parent."""
        parent_id = uuid4()
        event = MockAgentCreated(role="MANAGER", parent_id=parent_id)
        event.__class__.__name__ = "AgentCreated"

        result = EventFormatter.format(event)

        assert "MANAGER" in result
        assert "parent:" in result

    def test_format_task_assigned(self) -> None:
        """Test TaskAssigned event formatting."""
        event = MockTaskAssigned(task_description="Build a REST API")
        event.__class__.__name__ = "TaskAssigned"

        result = EventFormatter.format(event)

        assert "Task:" in result
        assert "Build a REST API" in result

    def test_format_task_assigned_truncates_long_task(self) -> None:
        """Test that long task descriptions are truncated."""
        long_task = "A" * 100
        event = MockTaskAssigned(task_description=long_task)
        event.__class__.__name__ = "TaskAssigned"

        result = EventFormatter.format(event)

        assert "..." in result
        assert len(result) < len(long_task) + 50

    def test_format_status_changed(self) -> None:
        """Test StatusChanged event formatting."""
        event = MockStatusChanged(old_status="pending", new_status="running")
        event.__class__.__name__ = "StatusChanged"

        result = EventFormatter.format(event)

        assert "pending" in result
        assert "running" in result
        assert "→" in result

    def test_format_status_changed_with_reason(self) -> None:
        """Test StatusChanged event with reason."""
        event = MockStatusChanged(
            old_status="pending",
            new_status="failed",
            reason="timeout"
        )
        event.__class__.__name__ = "StatusChanged"

        result = EventFormatter.format(event)

        assert "(timeout)" in result

    def test_format_complexity_evaluated_simple(self) -> None:
        """Test ComplexityEvaluated for simple task."""
        event = MockComplexityEvaluated(complexity="simple", determined_role="worker")
        event.__class__.__name__ = "ComplexityEvaluated"

        result = EventFormatter.format(event)

        assert "simple" in result
        assert "WORKER" in result
        assert "🔧" in result

    def test_format_complexity_evaluated_complex(self) -> None:
        """Test ComplexityEvaluated for complex task."""
        event = MockComplexityEvaluated(complexity="complex", determined_role="manager")
        event.__class__.__name__ = "ComplexityEvaluated"

        result = EventFormatter.format(event)

        assert "complex" in result
        assert "MANAGER" in result
        assert "🔀" in result

    def test_format_subtasks_defined(self) -> None:
        """Test SubtasksDefined event formatting."""
        event = MockSubtasksDefined(subtasks=[1, 2, 3])
        event.__class__.__name__ = "SubtasksDefined"

        result = EventFormatter.format(event)

        assert "Decomposed into 3 subtask(s)" in result

    def test_format_child_spawned(self) -> None:
        """Test ChildSpawned event formatting."""
        event = MockChildSpawned(child_role="worker")
        event.__class__.__name__ = "ChildSpawned"

        result = EventFormatter.format(event)

        assert "Spawned child" in result
        assert "worker" in result

    def test_format_child_spawned_with_subtask(self) -> None:
        """Test ChildSpawned event with subtask description."""
        subtask = MockSubtask(description="Process data from API")
        event = MockChildSpawned(child_role="worker", subtask=subtask)
        event.__class__.__name__ = "ChildSpawned"

        result = EventFormatter.format(event)

        assert "Process data" in result
        assert "└─" in result

    def test_format_code_generation_started(self) -> None:
        """Test CodeGenerationStarted event formatting."""
        event = MockCodeGenerationStarted(tool_name="claude_code")
        event.__class__.__name__ = "CodeGenerationStarted"

        result = EventFormatter.format(event)

        assert "Code generation started" in result
        assert "claude_code" in result

    def test_format_thought_captured_returns_none(self) -> None:
        """Test ThoughtCaptured events are silently skipped."""
        event = MockThoughtCaptured()
        event.__class__.__name__ = "ThoughtCaptured"

        result = EventFormatter.format(event)

        assert result is None

    def test_format_work_completed(self) -> None:
        """Test WorkCompleted event formatting."""
        event = MockWorkCompleted()
        event.__class__.__name__ = "WorkCompleted"

        result = EventFormatter.format(event)

        assert "Work completed" in result
        assert "✅" in result

    def test_format_work_failed(self) -> None:
        """Test WorkFailed event formatting."""
        event = MockWorkFailed(reason="Connection timeout")
        event.__class__.__name__ = "WorkFailed"

        result = EventFormatter.format(event)

        assert "Work failed" in result
        assert "Connection timeout" in result
        assert "❌" in result

    def test_format_child_completed(self) -> None:
        """Test ChildCompleted event formatting."""
        event = MockChildCompleted()
        event.__class__.__name__ = "ChildCompleted"

        result = EventFormatter.format(event)

        assert "Child" in result
        assert "completed" in result

    def test_format_unknown_event_uses_default(self) -> None:
        """Test that unknown events use default formatter."""
        event = MockEvent()
        event.__class__.__name__ = "UnknownEvent"

        result = EventFormatter.format(event)

        assert "UnknownEvent" in result


class TestProgressDisplayFormatter:
    """Tests for ProgressDisplayFormatter callback."""

    def test_display_prints_formatted_event(self) -> None:
        """Test that display prints formatted output."""
        event = MockWorkCompleted()
        event.__class__.__name__ = "WorkCompleted"

        captured = StringIO()
        with patch("builtins.print", side_effect=lambda x: captured.write(x + "\n")):
            ProgressDisplayFormatter.display(event, None)

        output = captured.getvalue()
        assert "Work completed" in output

    def test_display_skips_silent_events(self) -> None:
        """Test that display skips events that return None."""
        event = MockThoughtCaptured()
        event.__class__.__name__ = "ThoughtCaptured"

        captured = StringIO()
        with patch("builtins.print", side_effect=lambda x: captured.write(x + "\n")):
            ProgressDisplayFormatter.display(event, None)

        output = captured.getvalue()
        assert output == ""


class TestEventFormatterExtensibility:
    """Tests for EventFormatter extensibility (OCP compliance)."""

    def test_can_register_new_formatter(self) -> None:
        """Test that new formatters can be registered."""
        from presentation.formatters.event_formatter import (
            EventFormatter,
            EventFormatterStrategy,
        )

        class CustomEventFormatter(EventFormatterStrategy):
            def format(self, event: object) -> str:
                return "Custom: test"

        EventFormatter.register("CustomEvent", CustomEventFormatter())

        event = MockEvent()
        event.__class__.__name__ = "CustomEvent"

        result = EventFormatter.format(event)

        assert result == "Custom: test"
