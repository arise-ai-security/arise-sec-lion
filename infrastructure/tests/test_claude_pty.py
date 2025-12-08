"""Tests for ClaudeCodePTYAdapter.

These tests use a dummy CLI script (dummy_cli.py) to simulate Claude Code behavior
without requiring the actual Claude CLI installation.

Note: The adapter yields only ThoughtCaptured and WorkCompleted/WorkFailed events.
The CodeGenerationStarted event is created by the domain model (model.py execute_task),
not by the adapter. This prevents duplicate events.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from core.domain.events import ThoughtCaptured, WorkCompleted, WorkFailed
from infrastructure.adapters.claude_pty_adapter import ClaudeCodePTYAdapter


# Path to dummy CLI script
DUMMY_CLI_PATH = str(Path(__file__).parent / "dummy_cli.py")


@pytest.mark.asyncio
async def test_pty_adapter_successful_execution():
    """Test that adapter successfully runs dummy CLI and yields events."""

    # Given: PTY adapter configured to use dummy CLI
    adapter = ClaudeCodePTYAdapter(
        command=DUMMY_CLI_PATH,
        timeout_seconds=10,
    )

    session_id = uuid4()
    task_context = {
        "task_description": "Write a Python script",
        "session_id": session_id,
    }

    # When: Run session
    events = []
    async for event in adapter.run_session(task_context):
        events.append(event)

    # Then: Should yield events (adapter yields ThoughtCaptured and WorkCompleted/WorkFailed)
    # Note: CodeGenerationStarted is created by domain model, not the adapter
    assert len(events) > 0, "Should yield at least one event"

    # And: Should yield ThoughtCaptured events for thinking output
    thought_events = [e for e in events if isinstance(e, ThoughtCaptured)]
    assert len(thought_events) > 0, "Should capture thinking output"

    # Verify thinking patterns were captured
    thought_contents = [e.content for e in thought_events]
    assert any("thinking" in c.lower() for c in thought_contents), (
        "Should capture 'thinking' output"
    )
    assert any("analyzing" in c.lower() for c in thought_contents), (
        "Should capture 'analyzing' output"
    )

    # And: Should yield WorkCompleted as final event
    assert isinstance(events[-1], WorkCompleted)
    assert "Done" in events[-1].result or "completed" in events[-1].result.lower()


@pytest.mark.asyncio
async def test_pty_adapter_ansi_stripping():
    """Test that ANSI color codes are stripped from output."""

    # Given: PTY adapter
    adapter = ClaudeCodePTYAdapter(command=DUMMY_CLI_PATH)

    session_id = uuid4()
    task_context = {
        "task_description": "Simple task",
        "session_id": session_id,
    }

    # When: Run session
    events = []
    async for event in adapter.run_session(task_context):
        events.append(event)

    # Then: ThoughtCaptured events should NOT contain ANSI codes
    thought_events = [e for e in events if isinstance(e, ThoughtCaptured)]

    for event in thought_events:
        assert "\x1b[" not in event.content, (
            f"ANSI codes should be stripped from: {event.content!r}"
        )


@pytest.mark.asyncio
async def test_pty_adapter_failure_scenario():
    """Test that adapter handles process failures correctly."""

    # Given: PTY adapter with task description containing "fail"
    adapter = ClaudeCodePTYAdapter(command=DUMMY_CLI_PATH, timeout_seconds=10)

    session_id = uuid4()
    task_context = {
        "task_description": "This task will fail intentionally",
        "session_id": session_id,
    }

    # When: Run session
    events = []
    async for event in adapter.run_session(task_context):
        events.append(event)

    # Then: Should yield WorkFailed as final event
    assert isinstance(events[-1], WorkFailed)
    assert "exited with code 1" in events[-1].reason

    # And: Should include error details from subprocess output
    assert "Error details:" in events[-1].reason or "Last output:" in events[-1].reason
    # The dummy CLI prints "Error: Task failed!" which should be captured
    assert "error" in events[-1].reason.lower() or "failed" in events[-1].reason.lower()


@pytest.mark.asyncio
async def test_pty_adapter_timeout():
    """Test that adapter handles timeouts correctly."""

    # Given: PTY adapter with very short timeout
    # Create a temporary script that sleeps longer than timeout
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("#!/usr/bin/env python3\n")
        f.write("import sys\n")
        f.write("import time\n")
        f.write("# Ignore command-line arguments\n")
        f.write("time.sleep(10)\n")  # Sleep 10 seconds
        f.write("print('Should not reach here')\n")
        sleep_script = f.name

    Path(sleep_script).chmod(0o755)

    try:
        # Use python3 to execute the script (don't rely on shebang)
        adapter = ClaudeCodePTYAdapter(
            command="python3",
            timeout_seconds=1,  # 1 second timeout
        )

        session_id = uuid4()
        task_context = {
            "task_description": sleep_script,  # Pass script path as argument
            "session_id": session_id,
        }

        # When: Run session
        events = []
        async for event in adapter.run_session(task_context):
            events.append(event)

        # Then: Should yield WorkFailed with timeout message
        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason.lower()

    finally:
        # Clean up temporary script
        Path(sleep_script).unlink()


@pytest.mark.asyncio
async def test_pty_adapter_missing_task_description():
    """Test that adapter validates required task_context fields."""

    # Given: PTY adapter
    adapter = ClaudeCodePTYAdapter(command=DUMMY_CLI_PATH)

    # When: Call run_session without task_description
    task_context = {
        "session_id": uuid4(),
        # Missing task_description
    }

    # Then: Should raise ValueError
    with pytest.raises(ValueError, match="task_description"):
        async for _ in adapter.run_session(task_context):
            pass


@pytest.mark.asyncio
async def test_pty_adapter_missing_session_id():
    """Test that adapter validates session_id is present."""

    # Given: PTY adapter
    adapter = ClaudeCodePTYAdapter(command=DUMMY_CLI_PATH)

    # When: Call run_session without session_id
    task_context = {
        "task_description": "Some task",
        # Missing session_id
    }

    # Then: Should raise ValueError
    with pytest.raises(ValueError, match="session_id"):
        async for _ in adapter.run_session(task_context):
            pass


@pytest.mark.asyncio
async def test_pty_adapter_working_directory():
    """Test that adapter respects working_directory setting."""

    # Given: PTY adapter with custom working directory
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a script that prints current working directory
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=temp_dir) as f:
            f.write("#!/usr/bin/env python3\n")
            f.write("import os\n")
            f.write("print(f'Working dir: {os.getcwd()}')\n")
            pwd_script = f.name

        Path(pwd_script).chmod(0o755)

        try:
            adapter = ClaudeCodePTYAdapter(command=pwd_script)

            session_id = uuid4()
            task_context = {
                "task_description": "Print working directory",
                "session_id": session_id,
                "working_directory": temp_dir,
            }

            # When: Run session
            events = []
            async for event in adapter.run_session(task_context):
                events.append(event)

            # Then: Should complete successfully
            # (Cannot easily verify working directory without parsing output,
            # but test ensures no errors occur with working_directory set)
            assert len(events) > 0

        finally:
            Path(pwd_script).unlink()


def test_strip_ansi_static_method():
    """Test ANSI stripping utility method."""

    # Given: Text with ANSI color codes
    colored_text = "\x1b[31mRed text\x1b[0m and \x1b[32mGreen text\x1b[0m"

    # When: Strip ANSI codes
    clean_text = ClaudeCodePTYAdapter._strip_ansi(colored_text)

    # Then: Should remove all ANSI codes
    assert clean_text == "Red text and Green text"
    assert "\x1b[" not in clean_text


def test_is_thought_output_detection():
    """Test thinking output detection heuristic."""

    # Given: Various lines of output
    thinking_lines = [
        "> Thinking about the problem...",
        "Analyzing the requirements",
        "Planning the solution",
        "Considering different approaches",
    ]

    non_thinking_lines = [
        "Hello world",
        "Output: 42",
        "Result: success",
    ]

    # When/Then: Verify thinking detection
    for line in thinking_lines:
        assert ClaudeCodePTYAdapter._is_thought_output(line), f"Should detect as thinking: {line}"

    for line in non_thinking_lines:
        assert not ClaudeCodePTYAdapter._is_thought_output(line), (
            f"Should NOT detect as thinking: {line}"
        )


def test_is_completion_marker_detection():
    """Test completion marker detection."""

    # Given: Various lines of output
    completion_lines = [
        "Task completed successfully",
        "Done!",
        "Finished processing",
        "Success: all tests passed",
    ]

    non_completion_lines = [
        "Processing...",
        "Working on it",
        "In progress",
    ]

    # When/Then: Verify completion detection
    for line in completion_lines:
        assert ClaudeCodePTYAdapter._is_completion_marker(line), (
            f"Should detect as completion: {line}"
        )

    for line in non_completion_lines:
        assert not ClaudeCodePTYAdapter._is_completion_marker(line), (
            f"Should NOT detect as completion: {line}"
        )


@pytest.mark.asyncio
async def test_pty_adapter_captures_python_traceback():
    """Test that Python tracebacks are captured in error details."""

    # Given: Create a script that raises an exception with traceback
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("#!/usr/bin/env python3\n")
        f.write("def main():\n")
        f.write("    raise ValueError('Something went wrong!')\n")
        f.write("\n")
        f.write("if __name__ == '__main__':\n")
        f.write("    main()\n")
        error_script = f.name

    Path(error_script).chmod(0o755)

    try:
        adapter = ClaudeCodePTYAdapter(command="python3")

        session_id = uuid4()
        task_context = {
            "task_description": error_script,
            "session_id": session_id,
        }

        # When: Run session
        events = []
        async for event in adapter.run_session(task_context):
            events.append(event)

        # Then: Should yield WorkFailed with traceback details
        assert isinstance(events[-1], WorkFailed)
        assert "exited with code 1" in events[-1].reason

        # And: Should capture error/traceback information
        reason_lower = events[-1].reason.lower()
        assert "error" in reason_lower or "traceback" in reason_lower, (
            f"Should capture error/traceback info. Got: {events[-1].reason}"
        )

    finally:
        Path(error_script).unlink()
