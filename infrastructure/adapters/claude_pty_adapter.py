"""Claude Code PTY Adapter: wraps CLI in pseudo-terminal for real-time output capture."""

import asyncio
import contextlib
import os
import pty
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from core.domain.events import (
    DomainEvent,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.ports.worker_port import WorkerToolPort
from infrastructure.adapters.worker_base import validate_task_context


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


class ClaudeCodePTYAdapter(WorkerToolPort):
    """PTY adapter: spawn CLI, capture output, yield events."""

    def __init__(
        self,
        command: str = "claude",
        timeout_seconds: int = 300,
        buffer_size: int = 4096,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.buffer_size = buffer_size

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return ANSI_ESCAPE_PATTERN.sub("", text)

    @staticmethod
    def _is_thought_output(line: str) -> bool:
        line_lower = line.lower().strip()
        thinking_indicators = [
            line.startswith(">"),
            "thinking" in line_lower,
            "analyzing" in line_lower,
            "considering" in line_lower,
            "planning" in line_lower,
        ]
        return any(thinking_indicators)

    @staticmethod
    def _classify_output(line: str) -> str:
        """Classify line: 'thinking', 'progress', or 'output'."""
        line_lower = line.lower().strip()

        thinking_indicators = [
            line.startswith(">"),
            "thinking" in line_lower,
            "analyzing" in line_lower,
            "considering" in line_lower,
            "planning" in line_lower,
            "reasoning" in line_lower,
            "let me" in line_lower,
            "i'll" in line_lower,
            "i will" in line_lower,
        ]
        if any(thinking_indicators):
            return "thinking"

        progress_indicators = [
            "created" in line_lower,
            "modified" in line_lower,
            "deleted" in line_lower,
            "running" in line_lower,
            "executing" in line_lower,
            "reading" in line_lower,
            "writing" in line_lower,
            "installing" in line_lower,
            "building" in line_lower,
            "compiling" in line_lower,
            line.startswith(("✓", "→", "⠋", "⠙")),
        ]
        if any(progress_indicators):
            return "progress"

        return "output"

    async def _spawn_process(
        self, task_description: str, working_dir: str
    ) -> tuple[int, int, asyncio.subprocess.Process]:
        master_fd, slave_fd = pty.openpty()
        process = await asyncio.create_subprocess_exec(
            self.command,
            task_description,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=working_dir,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)
        return master_fd, slave_fd, process

    async def _read_output_with_timeout(
        self, master_fd: int, process: asyncio.subprocess.Process, session_id: Any
    ) -> tuple[list[DomainEvent], list[str]]:
        """Read PTY output, classify, and collect as ThoughtCaptured events."""
        output_buffer: list[str] = []
        sequence = 2

        async def read_all_output() -> list[DomainEvent]:
            nonlocal sequence
            collected_events: list[DomainEvent] = []

            async for output_chunk in self._read_pty_async(master_fd):
                for line in output_chunk.splitlines():
                    if not line.strip():
                        continue
                    output_type = self._classify_output(line)
                    collected_events.append(
                        ThoughtCaptured(
                            aggregate_id=session_id,
                            sequence_number=sequence,
                            content=line.strip(),
                            stream="stdout",
                            output_type=output_type,
                        )
                    )
                    sequence += 1
                    output_buffer.append(line)

            await process.wait()
            return collected_events

        events_list = await asyncio.wait_for(read_all_output(), timeout=self.timeout_seconds)
        return events_list, output_buffer

    @staticmethod
    def _format_error_reason(returncode: int, output_buffer: list[str]) -> str:
        error_markers = ["error", "exception", "failed", "traceback"]
        error_lines = [
            line
            for line in output_buffer
            if any(marker in line.lower() for marker in error_markers)
        ]

        if error_lines:
            error_detail = "\n".join(error_lines[:5])
            return f"Process exited with code {returncode}. Error details:\n{error_detail}"

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
        line_lower = line.lower().strip()
        completion_markers = [
            "done" in line_lower,
            "completed" in line_lower,
            "success" in line_lower,
            "finished" in line_lower,
        ]
        return any(completion_markers)

    async def _read_pty_async(self, master_fd: int) -> AsyncIterator[str]:
        """Async read from PTY master FD, yielding stripped output."""
        loop = asyncio.get_event_loop()

        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, self.buffer_size)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                clean_text = self._strip_ansi(text)
                if clean_text.strip():
                    yield clean_text
            except OSError:
                break

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Spawn CLI via PTY, stream ThoughtCaptured events, yield final WorkCompleted/Failed."""
        task_description, session_id, working_dir = validate_task_context(task_context)
        master_fd, _slave_fd, process = await self._spawn_process(task_description, working_dir)

        try:
            try:
                events_list, output_buffer = await self._read_output_with_timeout(
                    master_fd, process, session_id
                )

                for event in events_list:
                    yield event

                final_sequence = 2 + len(events_list)
                final_event = self._create_final_event(
                    process, output_buffer, session_id, final_sequence
                )
                yield final_event

            except TimeoutError:
                process.kill()
                yield WorkFailed(
                    aggregate_id=session_id,
                    sequence_number=2,
                    reason=f"Task timed out after {self.timeout_seconds} seconds",
                )

        except Exception as e:
            yield WorkFailed(
                aggregate_id=session_id,
                sequence_number=1,
                reason=f"PTY adapter error: {e!r}",
            )

        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)
