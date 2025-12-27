"""Claude Agent SDK Adapter: uses official SDK for structured tool execution.

Yields ThoughtCaptured events with precise output_type values based on SDK
message types, plus WorkCompleted/WorkFailed on completion.

Uses PostToolUse hooks for capturing tool invocations as events.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from core.domain.events import DomainEvent, ThoughtCaptured
from core.ports.worker_port import WorkerToolPort
from infrastructure.adapters.event_helpers import EventSequencer, format_tool_event
from infrastructure.adapters.worker_base import validate_task_context


PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]


@dataclass
class SDKAdapterConfig:
    """Configuration for Claude Agent SDK adapter."""

    model: str | None = None
    timeout_seconds: int = 300
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    )
    permission_mode: PermissionMode = "bypassPermissions"


class ClaudeAgentSDKAdapter(WorkerToolPort):
    """SDK-based adapter: structured message streaming with hook-based tool capture.

    Maps SDK message types to ThoughtCaptured output_type values:
    - TextBlock -> "output"
    - ThinkingBlock -> "thinking"
    - ToolUseBlock -> "tool_use" (via PostToolUse hook)
    - ToolResultBlock -> "tool_result"
    """

    STREAM_NAME = "claude_sdk"

    def __init__(self, config: SDKAdapterConfig | None = None) -> None:
        """Initialize SDK adapter.

        Args:
            config: Optional configuration. Uses defaults if not provided.
        """
        self.config = config or SDKAdapterConfig()

    async def run_session(
        self, task_context: dict[str, Any]
    ) -> AsyncIterator[DomainEvent]:
        """Execute task via SDK, stream ThoughtCaptured events, yield final event.

        Yields events as they are produced, not after completion.
        Uses PostToolUse hooks to capture tool invocations.
        """
        task_description, agent_id, working_dir = validate_task_context(task_context)
        sequencer = EventSequencer(agent_id, stream=self.STREAM_NAME)
        tool_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        try:
            options = self._build_options(working_dir, tool_queue)

            async with ClaudeSDKClient(options=options) as client:
                await client.query(task_description)

                async for message in client.receive_response():
                    # Yield pending tool events from hooks
                    async for event in _drain_queue(tool_queue, sequencer):
                        yield event

                    # Process message content blocks
                    async for event in _process_message(message, sequencer):
                        yield event

                    # Handle result message (terminal)
                    if isinstance(message, ResultMessage):
                        async for event in _drain_queue(tool_queue, sequencer):
                            yield event
                        yield _make_result_event(message, sequencer)
                        return

        except TimeoutError:
            yield sequencer.failed(
                f"Task timed out after {self.config.timeout_seconds} seconds"
            )

        except Exception as e:
            yield sequencer.failed(_format_error(e))

    def _build_options(
        self,
        working_dir: str,
        tool_queue: asyncio.Queue[tuple[str, str]],
    ) -> ClaudeAgentOptions:
        """Build SDK options with PostToolUse hook for event capture."""

        async def capture_tool_use(
            input_data: HookInput,
            _tool_use_id: str | None,
            _context: HookContext,
        ) -> SyncHookJSONOutput:
            """Hook callback to capture tool invocations."""
            tool_name = getattr(input_data, "tool_name", "unknown")
            tool_input = getattr(input_data, "tool_input", {})
            content = format_tool_event(tool_name, tool_input)
            await tool_queue.put((content, "tool_use"))
            return SyncHookJSONOutput()

        return ClaudeAgentOptions(
            model=self.config.model,
            cwd=working_dir,
            allowed_tools=self.config.allowed_tools,
            permission_mode=self.config.permission_mode,
            hooks={
                "PostToolUse": [HookMatcher(hooks=[capture_tool_use])],
            },
        )


# --- Module-level helper functions ---


async def _drain_queue(
    queue: asyncio.Queue[tuple[str, str]],
    sequencer: EventSequencer,
) -> AsyncIterator[ThoughtCaptured]:
    """Drain all pending events from queue."""
    while not queue.empty():
        content, output_type = queue.get_nowait()
        yield sequencer.thought(content, output_type)


async def _process_message(
    message: Any,
    sequencer: EventSequencer,
) -> AsyncIterator[ThoughtCaptured]:
    """Process SDK message and yield domain events."""
    if not isinstance(message, AssistantMessage):
        return

    for block in message.content:
        result = _process_block(block)
        if result:
            content, output_type = result
            yield sequencer.thought(content, output_type)


def _make_result_event(
    message: ResultMessage,
    sequencer: EventSequencer,
) -> DomainEvent:
    """Create terminal event from ResultMessage."""
    if getattr(message, "is_error", False):
        return sequencer.failed(getattr(message, "result", None) or "Task failed")
    return sequencer.completed(getattr(message, "result", None) or "Task completed")


def _process_block(block: Any) -> tuple[str, str] | None:
    """Process a single content block, returning (content, output_type) or None."""
    if isinstance(block, TextBlock):
        if block.text.strip():
            return block.text, "output"
    elif isinstance(block, ThinkingBlock):
        if block.thinking.strip():
            return block.thinking, "thinking"
    elif isinstance(block, ToolResultBlock):
        content = str(block.content)[:500] if block.content else "(no output)"
        return f"Tool result: {content}", "tool_result"
    return None


def _format_error(error: Exception) -> str:
    """Format exception as user-friendly error message."""
    error_str = str(error).lower()
    if "cli" in error_str and ("not found" in error_str or "missing" in error_str):
        return (
            "Claude Agent SDK CLI not found. "
            "The CLI is bundled with the SDK - try reinstalling: "
            "pip install --force-reinstall claude-agent-sdk"
        )
    return f"Claude SDK adapter error: {error!r}"
