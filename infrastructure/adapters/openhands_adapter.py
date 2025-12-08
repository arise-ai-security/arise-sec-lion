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

Event Streaming:
    The OpenHands SDK uses callback-based event streaming via `on_event(event: Event)`.
    SDK Event Types (per https://arxiv.org/html/2511.03690v1):
    - ActionEvent: Agent tool invocations with reasoning
    - MessageEvent: User/assistant text exchanges
    - ObservationEvent: Successful tool execution results
    - AgentErrorEvent: System or agent failures

TODO: APPLICATION LAYER INTEGRATION REQUIRED
---------------------------------------------
Same limitations as ClaudeCodePTYAdapter - sequence numbers are hardcoded.
See claude_pty_adapter.py for detailed explanation.
"""

import asyncio
import logging
import os
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


# Suppress verbose OpenHands SDK logging to prevent stdout pollution
# Events are captured as domain events instead
logging.getLogger("openhands").setLevel(logging.WARNING)
logging.getLogger("openhands.sdk").setLevel(logging.WARNING)
logging.getLogger("openhands.tools").setLevel(logging.WARNING)

# Map SDK event class names to our output_type classification
SDK_EVENT_TYPE_MAP: dict[str, str] = {
    # Agent reasoning and actions → thinking
    "ActionEvent": "thinking",
    "AgentThinkAction": "thinking",
    # Messages and observations → output
    "MessageEvent": "output",
    "ObservationEvent": "output",
    "CmdRunObservation": "output",
    # Errors → output (captured, not logged)
    "AgentErrorEvent": "output",
    # File operations → progress
    "FileReadAction": "progress",
    "FileWriteAction": "progress",
    "FileEditAction": "progress",
}


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

    @staticmethod
    def _classify_event(event: Any) -> str:
        """Classify an OpenHands SDK event into our output_type categories.

        Uses the SDK_EVENT_TYPE_MAP to map event class names to output_type.
        Falls back to "output" for unknown event types.

        Args:
            event: An OpenHands SDK event object.

        Returns:
            One of: "thinking", "progress", "output", "debug"
        """
        event_class_name = type(event).__name__
        return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")

    @staticmethod
    def _extract_event_content(event: Any) -> str | None:
        """Extract displayable content from an OpenHands SDK event.

        Args:
            event: An OpenHands SDK event object.

        Returns:
            String content or None if no content found.
        """
        # Try various common attributes for event content
        if hasattr(event, "message") and event.message:
            return str(event.message)
        if hasattr(event, "content") and event.content:
            return str(event.content)
        if hasattr(event, "action") and event.action:
            return f"Action: {event.action}"
        if hasattr(event, "observation") and event.observation:
            return f"Observation: {event.observation}"
        if hasattr(event, "thought") and event.thought:
            return f"Thought: {event.thought}"
        return None

    async def _run_with_sdk(
        self,
        task_description: str,
        working_dir: str,
    ) -> tuple[list[tuple[str, str]], str | None, str | None]:
        """Run task using OpenHands SDK.

        Args:
            task_description: The task to execute.
            working_dir: Working directory for the agent.

        Returns:
            Tuple of (classified_events, result, error) where classified_events
            is a list of (content, output_type) tuples.
        """
        classified_events: list[tuple[str, str]] = []
        result: str | None = None
        error: str | None = None

        try:
            # Import OpenHands SDK and tools
            # openhands-sdk: Core SDK (LLM, Agent, Conversation, Tool)
            # openhands-tools: Built-in tools (TerminalTool, FileEditorTool)
            from openhands.sdk import LLM, Agent, Conversation, Tool
            from openhands.tools.file_editor import FileEditorTool
            from openhands.tools.terminal import TerminalTool

            # Create LLM instance
            llm = LLM(model=self.model, api_key=self.api_key)

            # Create agent with standard tools for code generation
            # Tool(name=...) references the tool by its registered name
            agent = Agent(
                llm=llm,
                tools=[
                    Tool(name=TerminalTool.name),  # "terminal"
                    Tool(name=FileEditorTool.name),  # "file_editor"
                ],
            )

            # Create conversation with workspace
            conversation = Conversation(agent=agent, workspace=working_dir)

            # Send the task
            conversation.send_message(task_description)

            # Run the agent - use run_in_executor for sync API
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, conversation.run)

            # Collect and classify events from conversation
            # The SDK provides events with various attributes
            if hasattr(conversation, "events"):
                for event in conversation.events:
                    content = self._extract_event_content(event)
                    if content:
                        output_type = self._classify_event(event)
                        classified_events.append((content, output_type))

            result = f"Task completed successfully in workspace: {working_dir}"
            if classified_events:
                # Use last 5 output events as summary
                output_events = [c for c, t in classified_events if t == "output"]
                result = "\n".join(output_events[-5:]) if output_events else result

        except ImportError as e:
            error = (
                f"OpenHands packages not installed. "
                f"Install with: uv add openhands-sdk openhands-tools. Error: {e}"
            )
        except Exception as e:
            error = f"OpenHands execution failed: {e!r}"

        return classified_events, result, error

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

        # Note: CodeGenerationStarted event is created by domain model (model.py execute_task)
        # The adapter only needs to yield ThoughtCaptured and WorkCompleted/WorkFailed events

        try:
            # Step 2: Run with timeout
            classified_events, result, error = await asyncio.wait_for(
                self._run_with_sdk(task_description, working_dir),
                timeout=self.timeout_seconds,
            )

            # Step 3: Yield classified thought events
            sequence = 2
            for content, output_type in classified_events:
                if content.strip():
                    yield ThoughtCaptured(
                        aggregate_id=session_id,
                        sequence_number=sequence,
                        content=content.strip(),
                        stream="openhands",
                        output_type=output_type,
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
