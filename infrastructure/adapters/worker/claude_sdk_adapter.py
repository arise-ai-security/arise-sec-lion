"""Claude Agent SDK Adapter: uses official SDK for structured tool execution.

Yields ThoughtCaptured events with precise output_type values based on SDK
message types, plus WorkCompleted/WorkFailed on completion.

Uses PostToolUse hooks for capturing tool invocations as events.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from core.domain.events.events import DomainEvent, ThoughtCaptured

from .base import WorkerAdapterBase
from .shared import ContainerSessionContext, EventSequencer, format_tool_event


type PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]


@dataclass
class SDKAdapterConfig:
    """Configuration for Claude Agent SDK adapter."""

    model: str | None = None
    timeout_seconds: int = 300
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    )
    permission_mode: PermissionMode = "bypassPermissions"


class ClaudeAgentSDKAdapter(WorkerAdapterBase):
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
        super().__init__(timeout_seconds=self.config.timeout_seconds)

    def _get_tool_name(self) -> str:
        """Return tool identifier for cost tracking."""
        return "claude_code"

    async def _execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        """Execute task via SDK, stream ThoughtCaptured events, yield final event.

        Yields events as they are produced, not after completion.
        Uses PostToolUse hooks to capture tool invocations.
        """
        self._start_timing()
        tool_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        runtime_model = self._resolve_runtime_model(task_context, self.config.model)
        container_session = ContainerSessionContext.from_task_context(task_context)
        if container_session is not None:
            task_description = container_session.apply_task_prefix(
                task_description,
                auto_shell=True,
            )

        try:
            options = self._build_options(
                working_dir,
                tool_queue,
                container_session,
                runtime_model=runtime_model,
            )

            async with ClaudeSDKClient(options=options) as client:
                await client.query(task_description)

                async for message in client.receive_response():
                    # Yield pending tool events from hooks
                    async for event in self._drain_queue(tool_queue, sequencer):
                        yield event

                    # Process message content blocks
                    async for event in self._process_message(message, sequencer):
                        yield event

                    # Handle result message (terminal)
                    if isinstance(message, ResultMessage):
                        async for event in self._drain_queue(tool_queue, sequencer):
                            yield event

                        # Emit cost event before terminal event
                        cost_event = self._make_cost_event(
                            message,
                            sequencer,
                            runtime_model=runtime_model,
                        )
                        if cost_event:
                            yield cost_event

                        yield self._make_result_event(message, sequencer)
                        return

        except TimeoutError:
            yield sequencer.failed(
                f"Task timed out after {self.timeout_seconds} seconds"
            )

        except Exception as e:
            yield sequencer.failed(self._format_error(e))

    def _build_options(
        self,
        working_dir: str,
        tool_queue: asyncio.Queue[tuple[str, str]],
        container_session: ContainerSessionContext | None,
        runtime_model: str | None,
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

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: Any,
        ) -> PermissionResultAllow:
            if container_session is None:
                return PermissionResultAllow()
            return PermissionResultAllow(
                updated_input=container_session.translate_tool_input(
                    tool_name,
                    tool_input,
                )
            )

        return ClaudeAgentOptions(
            model=runtime_model,
            cwd=working_dir,
            allowed_tools=self.config.allowed_tools,
            permission_mode=self.config.permission_mode,
            can_use_tool=can_use_tool if container_session is not None else None,
            hooks={
                "PostToolUse": [HookMatcher(hooks=[capture_tool_use])],
            },
        )

    async def _drain_queue(
        self,
        queue: asyncio.Queue[tuple[str, str]],
        sequencer: EventSequencer,
    ) -> AsyncIterator[ThoughtCaptured]:
        """Drain all pending events from queue."""
        while not queue.empty():
            content, output_type = queue.get_nowait()
            yield sequencer.thought(content, output_type)

    async def _process_message(
        self,
        message: Any,
        sequencer: EventSequencer,
    ) -> AsyncIterator[ThoughtCaptured]:
        """Process SDK message and yield domain events."""
        if not isinstance(message, AssistantMessage):
            return

        for block in message.content:
            result = self._process_block(block)
            if result:
                content, output_type = result
                yield sequencer.thought(content, output_type)

    def _make_cost_event(
        self,
        message: ResultMessage,
        sequencer: EventSequencer,
        runtime_model: str | None,
    ) -> DomainEvent | None:
        """Create WorkerCostRecorded event from ResultMessage if cost data available."""
        cost_usd = getattr(message, "total_cost_usd", None)
        usage = getattr(message, "usage", None) or {}

        # Only emit if we have cost data
        if cost_usd is None and not usage:
            return None

        total_tokens = None
        if usage:
            total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        return sequencer.cost_recorded(
            tool_name=self._get_tool_name(),
            cost_usd=cost_usd or 0.0,
            duration_seconds=self._get_duration(),
            model=runtime_model,
            tokens=total_tokens,
        )

    def _make_result_event(
        self,
        message: ResultMessage,
        sequencer: EventSequencer,
    ) -> DomainEvent:
        """Create terminal event from ResultMessage."""
        if getattr(message, "is_error", False):
            return sequencer.failed(getattr(message, "result", None) or "Task failed")
        return sequencer.completed(getattr(message, "result", None) or "Task completed")

    @staticmethod
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

    def _format_error(self, error: Exception) -> str:
        """Format exception as user-friendly error message."""
        error_str = str(error).lower()
        if "cli" in error_str and ("not found" in error_str or "missing" in error_str):
            return (
                "Claude Agent SDK CLI not found. "
                "The CLI is bundled with the SDK - try reinstalling: "
                "pip install --force-reinstall claude-agent-sdk"
            )
        return f"Claude SDK adapter error: {error!r}"
