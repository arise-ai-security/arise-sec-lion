"""OpenHands SDK Adapter for Worker Tool execution.

This adapter uses the OpenHands Software Agent SDK to execute code generation
tasks. OpenHands is an open-source AI software development agent that supports
multiple LLM providers via LiteLLM (OpenAI, Anthropic, etc.).

Reference: https://docs.openhands.dev/sdk

Architecture Note:
    Unlike the Claude Code PTY adapter which wraps a CLI tool, this adapter
    uses the OpenHands SDK directly as a Python library. This provides:
    - Better integration with async Python
    - Direct access to events without PTY parsing
    - Cleaner error handling
    - Support for any LLM provider (OpenAI GPT-4, etc.)

TODO: APPLICATION LAYER INTEGRATION REQUIRED
---------------------------------------------
Same limitations as ClaudeCodePTYAdapter - sequence numbers are hardcoded.
See claude_pty_adapter.py for detailed explanation.
"""

import asyncio
import os
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


class OpenHandsAdapter(WorkerToolPort):
    """OpenHands SDK-based adapter for worker task execution.

    This adapter uses the OpenHands Software Agent SDK to:
    1. Create an agent with configurable LLM (OpenAI, Anthropic, etc.)
    2. Run tasks in a workspace directory
    3. Capture agent thoughts and actions as domain events
    4. Stream events in real-time during execution

    The adapter supports any LLM provider supported by LiteLLM, making it
    flexible for users who don't have Anthropic API keys.

    Attributes:
        model: LLM model identifier (e.g., "openai/gpt-4o").
        api_key: API key for the LLM provider.
        timeout_seconds: Maximum execution time.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        """Initialize the OpenHands adapter.

        Args:
            model: LLM model identifier (e.g., "openai/gpt-4o", "anthropic/claude-3-5-sonnet").
                   If None, uses LLM_MODEL environment variable.
            api_key: API key for the LLM provider.
                     If None, uses LLM_API_KEY or provider-specific env var (OPENAI_API_KEY).
            timeout_seconds: Maximum execution time in seconds (default: 300).
        """
        self.model = model or os.getenv("LLM_MODEL", "openai/gpt-4o")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.timeout_seconds = timeout_seconds

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

    async def _run_with_sdk(
        self,
        task_description: str,
        working_dir: str,
    ) -> tuple[list[str], str | None, str | None]:
        """Run task using OpenHands SDK.

        Args:
            task_description: The task to execute.
            working_dir: Working directory for the agent.

        Returns:
            Tuple of (thought_logs, result, error).
        """
        thought_logs: list[str] = []
        result: str | None = None
        error: str | None = None

        try:
            # Import OpenHands SDK (lazy import to avoid import errors if not installed)
            from openhands.sdk import LLM, Agent, Conversation, Tool
            from openhands.tools.file_editor import FileEditorTool
            from openhands.tools.terminal import TerminalTool

            # Create LLM instance
            llm = LLM(model=self.model, api_key=self.api_key)

            # Create agent with standard tools for code generation
            agent = Agent(
                llm=llm,
                tools=[
                    Tool(name=TerminalTool.name),
                    Tool(name=FileEditorTool.name),
                ],
            )

            # Create conversation with workspace
            conversation = Conversation(agent=agent, workspace=working_dir)

            # Send the task
            conversation.send_message(task_description)

            # Run the agent (this is blocking in current SDK)
            # We run it in an executor to not block the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, conversation.run)

            # Collect thoughts from conversation events
            # Note: The exact API for accessing events may vary by SDK version
            if hasattr(conversation, "events"):
                for event in conversation.events:
                    if hasattr(event, "message"):
                        thought_logs.append(str(event.message))
                    elif hasattr(event, "content"):
                        thought_logs.append(str(event.content))
                    elif hasattr(event, "action"):
                        thought_logs.append(f"Action: {event.action}")
                    elif hasattr(event, "observation"):
                        thought_logs.append(f"Observation: {event.observation}")

            result = f"Task completed successfully in workspace: {working_dir}"
            if thought_logs:
                result = "\n".join(thought_logs[-5:])  # Last 5 thoughts as summary

        except ImportError as e:
            error = f"OpenHands SDK not installed. Install with: uv add openhands-sdk. Error: {e}"
        except Exception as e:
            error = f"OpenHands execution failed: {e!r}"

        return thought_logs, result, error

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Execute a task using OpenHands SDK and stream domain events.

        This method:
        1. Validates task context
        2. Initializes OpenHands agent with configured LLM
        3. Runs the task with timeout
        4. Yields events as they occur

        TODO: APPLICATION LAYER ORCHESTRATION NEEDED
        Same limitations as ClaudeCodePTYAdapter regarding sequence numbers.

        Args:
            task_context: Context for task execution. Expected keys:
                - task_description: str (the task to execute)
                - session_id: UUID (the agent session ID)
                - working_directory: str (optional, default: current dir)

        Yields:
            DomainEvent instances:
            - CodeGenerationStarted (when agent starts)
            - ThoughtCaptured (for agent thoughts/actions)
            - WorkCompleted (on successful completion)
            - WorkFailed (on errors or timeouts)

        Raises:
            ValueError: If required task_context fields are missing.
        """
        # Step 1: Validate inputs
        task_description, session_id, working_dir = self._validate_task_context(task_context)

        # Yield start event
        # TODO: APPLICATION LAYER should assign sequence_number
        yield CodeGenerationStarted(
            aggregate_id=session_id,
            sequence_number=1,
            tool_name=f"openhands ({self.model})",
        )

        try:
            # Step 2: Run with timeout
            thought_logs, result, error = await asyncio.wait_for(
                self._run_with_sdk(task_description, working_dir),
                timeout=self.timeout_seconds,
            )

            # Step 3: Yield thought events
            sequence = 2
            for thought in thought_logs:
                if thought.strip():
                    yield ThoughtCaptured(
                        aggregate_id=session_id,
                        sequence_number=sequence,
                        content=thought.strip(),
                        stream="openhands",
                    )
                    sequence += 1

            # Step 4: Yield final event
            if error:
                yield WorkFailed(
                    aggregate_id=session_id,
                    sequence_number=sequence,
                    reason=error,
                )
            else:
                yield WorkCompleted(
                    aggregate_id=session_id,
                    sequence_number=sequence,
                    result=result or "Task completed",
                )

        except TimeoutError:
            yield WorkFailed(
                aggregate_id=session_id,
                sequence_number=2,
                reason=f"Task timed out after {self.timeout_seconds} seconds",
            )

        except Exception as e:
            yield WorkFailed(
                aggregate_id=session_id,
                sequence_number=2,
                reason=f"OpenHands adapter error: {e!r}",
            )
