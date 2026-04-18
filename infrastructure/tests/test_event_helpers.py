"""Tests for event helpers module.

Tests EventSequencer and format_tool_event utilities.
"""

from uuid import uuid4

from core.domain.events.events import ThoughtCaptured, WorkCompleted, WorkFailed
from infrastructure.adapters.worker.shared import (
    TOOL_FORMATTERS,
    EventSequencer,
    format_tool_event,
)


class TestEventSequencer:
    """Tests for EventSequencer class."""

    def test_initial_sequence_defaults_to_2(self) -> None:
        """Sequence starts at 2 by default (1 reserved for CodeGenerationStarted)."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test")

        assert sequencer.current_sequence == 2

    def test_custom_start_sequence(self) -> None:
        """Can specify custom starting sequence."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test", start_sequence=10)

        assert sequencer.current_sequence == 10

    def test_thought_creates_event_with_correct_fields(self) -> None:
        """thought() creates ThoughtCaptured with correct fields."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="claude_sdk")

        event = sequencer.thought("Processing data", "thinking")

        assert isinstance(event, ThoughtCaptured)
        assert event.aggregate_id == agent_id
        assert event.sequence_number == 2
        assert event.content == "Processing data"
        assert event.stream == "claude_sdk"
        assert event.output_type == "thinking"

    def test_thought_increments_sequence(self) -> None:
        """Each thought() call increments the sequence number."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test")

        event1 = sequencer.thought("First", "thinking")
        event2 = sequencer.thought("Second", "output")
        event3 = sequencer.thought("Third", "progress")

        assert event1.sequence_number == 2
        assert event2.sequence_number == 3
        assert event3.sequence_number == 4
        assert sequencer.current_sequence == 5

    def test_completed_creates_event_with_correct_fields(self) -> None:
        """completed() creates WorkCompleted with correct fields."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test")

        # Emit some thoughts first
        sequencer.thought("Working...", "thinking")
        sequencer.thought("Still working...", "thinking")

        event = sequencer.completed("Task done!")

        assert isinstance(event, WorkCompleted)
        assert event.aggregate_id == agent_id
        assert event.sequence_number == 4  # After 2 thoughts
        assert event.result == "Task done!"

    def test_completed_does_not_increment_sequence(self) -> None:
        """completed() is terminal and doesn't increment sequence."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test")

        sequencer.completed("Done")

        # Sequence should not change after terminal event
        assert sequencer.current_sequence == 2

    def test_failed_creates_event_with_correct_fields(self) -> None:
        """failed() creates WorkFailed with correct fields."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test")

        event = sequencer.failed("Something went wrong")

        assert isinstance(event, WorkFailed)
        assert event.aggregate_id == agent_id
        assert event.sequence_number == 2
        assert event.reason == "Something went wrong"

    def test_failed_does_not_increment_sequence(self) -> None:
        """failed() is terminal and doesn't increment sequence."""
        agent_id = uuid4()
        sequencer = EventSequencer(agent_id, stream="test")

        sequencer.failed("Error")

        assert sequencer.current_sequence == 2

    def test_different_streams(self) -> None:
        """Stream identifier is correctly set on events."""
        agent_id = uuid4()

        sdk_sequencer = EventSequencer(agent_id, stream="claude_sdk")
        oh_sequencer = EventSequencer(agent_id, stream="openhands")

        sdk_event = sdk_sequencer.thought("Test", "output")
        oh_event = oh_sequencer.thought("Test", "output")

        assert sdk_event.stream == "claude_sdk"
        assert oh_event.stream == "openhands"

    def test_thought_sanitizes_null_bytes_in_tool_input_json(self) -> None:
        """JSONB-unsafe null bytes in tool_input_json are stripped before event construction."""
        # Given: a sequencer and tool_input with embedded null bytes in nested values
        seq = EventSequencer(agent_id=uuid4(), stream="test")
        dirty_input = {
            "command": "grep \x00pattern file",
            "nested": {"path": "/src/foo\x00bar"},
            "list_arg": ["clean", "dir\x00ty", 42],
        }

        # When: a tool_use thought is constructed
        event = seq.thought(
            content="Tool use",
            output_type="tool_use",
            call_id="tu_1",
            tool_input_json=dirty_input,
        )

        # Then: all null bytes are stripped from nested strings
        assert event.tool_input_json is not None
        assert event.tool_input_json["command"] == "grep pattern file"
        assert event.tool_input_json["nested"]["path"] == "/src/foobar"
        # And: non-string scalars (int) are preserved unchanged
        assert event.tool_input_json["list_arg"] == ["clean", "dirty", 42]

    def test_thought_passes_none_tool_input_json_through(self) -> None:
        """None tool_input_json stays None (no false dict materialization)."""
        # Given: a sequencer
        seq = EventSequencer(agent_id=uuid4(), stream="test")

        # When: thought is constructed without tool_input_json
        event = seq.thought(content="just text", output_type="output")

        # Then: tool_input_json remains None
        assert event.tool_input_json is None


class TestFormatToolEvent:
    """Tests for format_tool_event function."""

    def test_bash_with_description(self) -> None:
        """Bash tool prefers description over command."""
        result = format_tool_event("Bash", {
            "command": "ls -la /very/long/path",
            "description": "List files",
        })
        assert result == "Running: List files"

    def test_bash_without_description(self) -> None:
        """Bash tool falls back to command when no description."""
        result = format_tool_event("Bash", {"command": "ls -la"})
        assert result == "Running: ls -la"

    def test_bash_truncates_long_commands(self) -> None:
        """Bash tool truncates commands over 100 chars."""
        long_cmd = "x" * 150
        result = format_tool_event("Bash", {"command": long_cmd})
        assert len(result) < 120  # "Running: " + 100 chars + "..."
        assert result.endswith("...")

    def test_write_tool(self) -> None:
        """Write tool shows file path."""
        result = format_tool_event("Write", {"file_path": "/path/to/file.py"})
        assert result == "Writing: /path/to/file.py"

    def test_edit_tool(self) -> None:
        """Edit tool shows file path."""
        result = format_tool_event("Edit", {"file_path": "/path/to/file.py"})
        assert result == "Editing: /path/to/file.py"

    def test_read_tool(self) -> None:
        """Read tool shows file path."""
        result = format_tool_event("Read", {"file_path": "/path/to/file.py"})
        assert result == "Reading: /path/to/file.py"

    def test_glob_tool(self) -> None:
        """Glob tool shows pattern."""
        result = format_tool_event("Glob", {"pattern": "**/*.py"})
        assert result == "Searching files: **/*.py"

    def test_grep_tool(self) -> None:
        """Grep tool shows pattern."""
        result = format_tool_event("Grep", {"pattern": "TODO"})
        assert result == "Searching content: TODO"

    def test_unknown_tool(self) -> None:
        """Unknown tools fall back to generic format."""
        result = format_tool_event("CustomTool", {"key": "value"})
        assert result == "Tool: CustomTool"

    def test_missing_keys_handled_gracefully(self) -> None:
        """Missing keys don't raise errors."""
        result = format_tool_event("Read", {})
        assert result == "Reading: "

        result = format_tool_event("Bash", {})
        assert result == "Running: "


class TestToolFormattersRegistry:
    """Tests for TOOL_FORMATTERS registry."""

    def test_registry_contains_expected_tools(self) -> None:
        """Registry contains all expected tool formatters."""
        expected_tools = {
            # Claude Code tools
            "Bash", "Write", "Edit", "Read", "Glob", "Grep",
            # MCP filesystem tools
            "read_file", "write_file", "list_directory", "create_directory",
            # Shell execution tools
            "execute_command", "shell_execute",
        }
        assert expected_tools == set(TOOL_FORMATTERS.keys())

    def test_registry_is_extensible(self) -> None:
        """Registry can be extended with new tools."""
        original_count = len(TOOL_FORMATTERS)

        # Extension point - users can add custom formatters
        TOOL_FORMATTERS["WebSearch"] = lambda i: f"Searching: {i.get('query', '')}"

        result = format_tool_event("WebSearch", {"query": "python tutorials"})
        assert result == "Searching: python tutorials"

        # Cleanup
        del TOOL_FORMATTERS["WebSearch"]
        assert len(TOOL_FORMATTERS) == original_count
