"""Claude Code PTY Adapter for Worker Tool execution.

This adapter wraps Claude Code CLI in a pseudo-terminal (PTY) to capture
real-time output and convert it into domain events. The PTY approach is
necessary because Claude Code outputs ANSI-colored interactive content
that wouldn't be captured properly via standard subprocess pipes.

TODO: APPLICATION LAYER INTEGRATION REQUIRED
---------------------------------------------
ARCHITECTURAL LIMITATION:
This infrastructure adapter currently violates separation of concerns by:
1. Hardcoding sequence numbers in domain events
2. Assuming it's the only component appending events to the aggregate

IMPACT ON MULTI-WORKER SYSTEMS:
- Cannot handle concurrent workers (sequence conflicts)
- Cannot manage worker dependencies
- Breaks Optimistic Concurrency Control (OCC)

REQUIRED REFACTORING:
Create an Application Layer service (e.g., WorkerExecutionService) that:
1. Loads aggregate to get current version
2. Calls this adapter for raw events
3. Assigns proper sequence numbers based on aggregate state
4. Appends to event store with OCC
5. Handles worker dependencies and coordination

See individual TODO comments throughout this file for specific issues.
"""

import asyncio
import contextlib
import os
import pty
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from core.domain.events import (
    CodeGenerationStarted,
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.ports.worker_port import WorkerToolPort


# ANSI color code regex pattern
# Matches escape sequences like \x1b[31m, \x1b[0m, etc.
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


class ClaudeCodePTYAdapter(WorkerToolPort):
    """PTY-based adapter for Claude Code CLI with real-time event streaming.

    This adapter:
    1. Creates a PTY (pseudo-terminal) pair for interactive CLI execution
    2. Spawns Claude Code CLI attached to the PTY slave
    3. Reads output from the master FD asynchronously
    4. Strips ANSI color codes from output
    5. Parses output and yields domain events in real-time

    The adapter acts as the "ear" listening to Claude Code's thoughts and
    the "mouth" sending task descriptions.
    """

    def __init__(
        self,
        command: str = "claude",
        timeout_seconds: int = 300,
        buffer_size: int = 4096,
    ) -> None:
        """Initialize the Claude Code PTY adapter.

        Args:
            command: The CLI command to execute (default: "claude").
            timeout_seconds: Maximum execution time in seconds (default: 300).
            buffer_size: Buffer size for reading from PTY (default: 4096).
        """
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.buffer_size = buffer_size

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip ANSI color codes from text.

        Args:
            text: Text potentially containing ANSI escape sequences.

        Returns:
            Clean text with ANSI codes removed.
        """
        return ANSI_ESCAPE_PATTERN.sub("", text)

    @staticmethod
    def _is_thought_output(line: str) -> bool:
        """Heuristic to detect if a line represents "thinking" output.

        Claude Code typically outputs thinking with patterns like:
        - Lines starting with "> "
        - Lines containing "Thinking..."
        - Lines with specific markers

        Args:
            line: A single line of output.

        Returns:
            True if line appears to be thinking/reasoning output.
        """
        line_lower = line.lower().strip()

        # Check for common thinking patterns
        thinking_indicators = [
            line.startswith(">"),  # Claude Code prompt marker
            "thinking" in line_lower,
            "analyzing" in line_lower,
            "considering" in line_lower,
            "planning" in line_lower,
            # Add more patterns as needed
        ]

        return any(thinking_indicators)

    @staticmethod
    def _validate_task_context(task_context: dict[str, Any]) -> tuple[str, Any, str]:
        """Validate task context and extract required fields.

        Args:
            task_context: Context dictionary to validate.

        Returns:
            Tuple of (task_description, session_id, working_directory).

        Raises:
            ValueError: If required fields are missing.
        """
        task_description = task_context.get("task_description")
        session_id = task_context.get("session_id")

        if not task_description:
            raise ValueError("task_context must include 'task_description'")
        if not session_id:
            raise ValueError("task_context must include 'session_id'")

        working_dir = task_context.get("working_directory", str(Path.cwd()))

        return task_description, session_id, working_dir

    async def _spawn_process(
        self, task_description: str, working_dir: str
    ) -> tuple[int, int, asyncio.subprocess.Process]:
        """Create PTY pair and spawn subprocess.

        Args:
            task_description: Task to pass to subprocess as argument.
            working_dir: Working directory for subprocess.

        Returns:
            Tuple of (master_fd, slave_fd, process).
        """
        # Create PTY pair
        master_fd, slave_fd = pty.openpty()

        # Spawn process attached to PTY slave
        process = await asyncio.create_subprocess_exec(
            self.command,
            task_description,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=working_dir,
            preexec_fn=os.setsid,
        )

        # Close slave FD in parent process (child has its own copy)
        os.close(slave_fd)

        return master_fd, slave_fd, process

    async def _read_output_with_timeout(
        self, master_fd: int, process: asyncio.subprocess.Process, session_id: Any
    ) -> tuple[list[DomainEvent], list[str]]:
        """Read subprocess output with timeout and collect events.

        TODO: APPLICATION LAYER should manage sequence numbers
        This method hardcodes sequence starting at 2, assuming CodeGenerationStarted was #1.
        Application layer should assign these based on aggregate's current version.

        Args:
            master_fd: Master file descriptor of PTY.
            process: Subprocess to read from.
            session_id: Session ID for event generation.

        Returns:
            Tuple of (events_list, output_buffer).

        Raises:
            TimeoutError: If timeout is exceeded.
        """
        output_buffer: list[str] = []
        sequence = 2  # TODO: Remove hardcoded sequence - should be managed by application layer

        async def read_all_output() -> list[DomainEvent]:
            """Read all output and collect events/lines."""
            nonlocal sequence
            collected_events: list[DomainEvent] = []

            async for output_chunk in self._read_pty_async(master_fd):
                for line in output_chunk.splitlines():
                    if not line.strip():
                        continue

                    if self._is_thought_output(line):
                        # TODO: APPLICATION LAYER should assign sequence numbers
                        collected_events.append(
                            ThoughtCaptured(
                                aggregate_id=session_id,
                                sequence_number=sequence,  # TODO: Hardcoded increment
                                content=line.strip(),
                                stream="stdout",
                            )
                        )
                        sequence += 1  # TODO: Remove - application layer manages this

                    output_buffer.append(line)

            await process.wait()
            return collected_events

        events_list = await asyncio.wait_for(read_all_output(), timeout=self.timeout_seconds)
        return events_list, output_buffer

    @staticmethod
    def _format_error_reason(returncode: int, output_buffer: list[str]) -> str:
        """Format error reason from subprocess output.

        Args:
            returncode: Process exit code.
            output_buffer: Captured output lines.

        Returns:
            Formatted error message with context.
        """
        error_markers = ["error", "exception", "failed", "traceback"]
        error_lines = [
            line
            for line in output_buffer
            if any(marker in line.lower() for marker in error_markers)
        ]

        if error_lines:
            # Include specific error messages
            error_detail = "\n".join(error_lines[:5])
            return f"Process exited with code {returncode}. Error details:\n{error_detail}"

        # Include last few lines of output as context
        context_lines = output_buffer[-5:] if len(output_buffer) > 5 else output_buffer
        context = "\n".join(context_lines) if context_lines else "(no output)"
        return f"Process exited with code {returncode}. Last output:\n{context}"

    def _create_final_event(
        self,
        process: asyncio.subprocess.Process,
        output_buffer: list[str],
        session_id: Any,
        sequence: int,
    ) -> DomainEvent:
        """Create final event based on process exit code.

        Args:
            process: Completed subprocess.
            output_buffer: Captured output lines.
            session_id: Session ID for event.
            sequence: Sequence number for event.

        Returns:
            WorkCompleted or WorkFailed event.
        """
        if process.returncode == 0:
            result = "\n".join(output_buffer) if output_buffer else "Task completed successfully"
            return WorkCompleted(
                aggregate_id=session_id,
                sequence_number=sequence,
                result=result,
            )

        reason = self._format_error_reason(process.returncode, output_buffer)
        return WorkFailed(
            aggregate_id=session_id,
            sequence_number=sequence,
            reason=reason,
        )

    @staticmethod
    def _is_completion_marker(line: str) -> bool:
        """Detect if a line indicates successful completion.

        Args:
            line: A single line of output.

        Returns:
            True if line indicates task completion.
        """
        line_lower = line.lower().strip()

        completion_markers = [
            "done" in line_lower,
            "completed" in line_lower,
            "success" in line_lower,
            "finished" in line_lower,
        ]

        return any(completion_markers)

    async def _read_pty_async(self, master_fd: int) -> AsyncIterator[str]:
        """Read from PTY master file descriptor asynchronously.

        Args:
            master_fd: The master file descriptor of the PTY.

        Yields:
            Lines of output from the PTY (ANSI codes stripped).
        """
        loop = asyncio.get_event_loop()

        while True:
            try:
                # Read from master FD in non-blocking mode
                data = await loop.run_in_executor(None, os.read, master_fd, self.buffer_size)

                if not data:
                    # EOF reached
                    break

                # Decode and strip ANSI codes
                text = data.decode("utf-8", errors="replace")
                clean_text = self._strip_ansi(text)

                # Yield non-empty lines
                if clean_text.strip():
                    yield clean_text

            except OSError:
                # PTY closed or error occurred
                break

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Execute a Claude Code session via PTY and stream domain events.

        This method orchestrates the PTY workflow:
        1. Validates task context
        2. Spawns subprocess with PTY
        3. Reads output with timeout
        4. Yields events in real-time
        5. Returns final success/failure event

        TODO: APPLICATION LAYER ORCHESTRATION NEEDED
        ----------------------------------------------
        This infrastructure adapter currently:
        - Hardcodes sequence numbers (1, 2, 3...)
        - Directly creates domain events with sequence numbers

        REFACTORING REQUIRED:
        1. Create Application Layer service (WorkerExecutionService)
        2. Application layer should:
           - Load aggregate to get current version
           - Call this adapter to get raw events
           - Assign proper sequence numbers based on aggregate state
           - Append to event store with Optimistic Concurrency Control
           - Handle dependencies between workers

        3. This adapter should either:
           Option A: Yield events with placeholder sequence (0)
           Option B: Yield "raw" event DTOs without sequence numbers

        See: https://github.com/your-repo/docs/application-layer-design.md

        Args:
            task_context: Context for task execution. Expected keys:
                - task_description: str (the task to execute)
                - session_id: UUID (the agent session ID)
                - working_directory: str (optional, default: current dir)

        Yields:
            DomainEvent instances:
            - CodeGenerationStarted (when CLI starts)
            - ThoughtCaptured (for thinking/logging output)
            - WorkCompleted (on successful completion)
            - WorkFailed (on errors or timeouts)

        Raises:
            ValueError: If required task_context fields are missing.
        """
        # Step 1: Validate inputs
        task_description, session_id, working_dir = self._validate_task_context(task_context)

        # Step 2: Spawn process with PTY
        master_fd, _slave_fd, process = await self._spawn_process(task_description, working_dir)

        try:
            # Yield start event
            # TODO: APPLICATION LAYER should assign sequence_number based on aggregate state
            # This hardcoded value (1) assumes this is the first event, which may not be true
            # in a multi-worker system where the aggregate already has events.
            yield CodeGenerationStarted(
                aggregate_id=session_id,
                sequence_number=1,  # TODO: Remove hardcoded value, use placeholder (0) or raw event
                tool_name=self.command,
            )

            # Step 3: Read output with timeout
            try:
                events_list, output_buffer = await self._read_output_with_timeout(
                    master_fd, process, session_id
                )

                # Yield all thinking events
                for event in events_list:
                    yield event

                # Step 4: Create and yield final event
                # TODO: APPLICATION LAYER should calculate final sequence based on aggregate state
                # This calculation assumes sequence starts at 1, which breaks with multiple workers
                final_sequence = 2 + len(events_list)  # TODO: Remove calculation
                final_event = self._create_final_event(
                    process, output_buffer, session_id, final_sequence
                )
                yield final_event

            except TimeoutError:
                # Handle timeout
                process.kill()
                # TODO: APPLICATION LAYER should assign sequence number
                yield WorkFailed(
                    aggregate_id=session_id,
                    sequence_number=2,  # TODO: Hardcoded - should be from application layer
                    reason=f"Task timed out after {self.timeout_seconds} seconds",
                )

        except Exception as e:
            # Handle unexpected errors
            # TODO: APPLICATION LAYER should assign sequence number
            yield WorkFailed(
                aggregate_id=session_id,
                sequence_number=1,  # TODO: Hardcoded - should be from application layer
                reason=f"PTY adapter error: {e!r}",
            )

        finally:
            # Clean up resources
            with contextlib.suppress(OSError):
                os.close(master_fd)
