"""Claude Agent SDK Adapter: uses official SDK for structured tool execution.

Yields ThoughtCaptured events with precise output_type values based on SDK
message types, plus WorkCompleted/WorkFailed on completion.

Uses PostToolUse hooks for capturing tool invocations as events.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from time import time
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
from .shared import (
    ContainerSessionContext,
    EventSequencer,
    format_tool_event,
    to_sdk_mcp_servers,
)


type PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
type ThinkingDisplay = Literal["summarized", "omitted"]
type ToolEvent = tuple[str, str, str | None]
type ToolEventQueue = asyncio.Queue[ToolEvent]
type ToolUseHook = Callable[
    [HookInput, str | None, HookContext],
    Awaitable[SyncHookJSONOutput],
]
type ToolPermissionCallback = Callable[
    [str, dict[str, Any], Any],
    Awaitable[PermissionResultAllow],
]

DEFAULT_MAX_THINKING_TOKENS = 63_999


@dataclass
class SDKAdapterConfig:
    """Configuration for Claude Agent SDK adapter."""

    model: str | None = None
    timeout_seconds: int = 300
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    )
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: PermissionMode = "bypassPermissions"
    max_thinking_tokens: int | None = DEFAULT_MAX_THINKING_TOKENS
    thinking_display: ThinkingDisplay | None = "summarized"


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
        started_at = time()
        tool_queue: ToolEventQueue = asyncio.Queue()
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
                mcp_servers=self._extract_mcp_servers(task_context),
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
                            duration_seconds=time() - started_at,
                        )
                        if cost_event:
                            yield cost_event

                        yield self._make_result_event(message, sequencer)
                        return

        except TimeoutError:
            yield sequencer.failed(f"Task timed out after {self.timeout_seconds} seconds")

        except Exception as e:
            yield sequencer.failed(self._format_error(e))

    def _build_options(
        self,
        working_dir: str,
        tool_queue: ToolEventQueue,
        container_session: ContainerSessionContext | None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
    ) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "cwd": working_dir,
            "allowed_tools": list(self.config.allowed_tools),
            "disallowed_tools": list(self.config.disallowed_tools),
            "permission_mode": self.config.permission_mode,
            "can_use_tool": self._make_permission_callback(container_session),
            "hooks": {
                "PostToolUse": [
                    HookMatcher(hooks=[self._make_tool_use_hook(tool_queue)])
                ],
            },
        }
        if self.config.thinking_display is not None:
            kwargs["extra_args"] = {"thinking-display": self.config.thinking_display}
        if self.config.max_thinking_tokens is not None:
            kwargs["max_thinking_tokens"] = self.config.max_thinking_tokens
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        return ClaudeAgentOptions(**kwargs)

    @staticmethod
    def _make_tool_use_hook(tool_queue: ToolEventQueue) -> ToolUseHook:
        async def capture_tool_use(
            input_data: HookInput,
            _tool_use_id: str | None,
            _context: HookContext,
        ) -> SyncHookJSONOutput:
            tool_name = getattr(input_data, "tool_name", "unknown")
            tool_input = getattr(input_data, "tool_input", {})
            # Mirror claude_code_worker.py:565-567 and openhands_adapter.py:692:
            # append `\nInput: {<json>}` so downstream parsers that expect the
            # JSON-encoded tool input alongside the description (e.g.,
            # bash-command recovery) can find it.
            detail = json.dumps(tool_input, sort_keys=True, default=str)
            content = f"{format_tool_event(tool_name, tool_input)}\nInput: {detail}"
            # Audit N-6: pass the canonical tool_name through so the
            # downstream ThoughtCaptured event has a structured field
            # instead of forcing offline metrics to parse the human prefix.
            await tool_queue.put((content, "tool_use", tool_name))
            return SyncHookJSONOutput()

        return capture_tool_use

    @staticmethod
    def _make_permission_callback(
        container_session: ContainerSessionContext | None,
    ) -> ToolPermissionCallback | None:
        if container_session is None:
            return None

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: Any,
        ) -> PermissionResultAllow:
            return PermissionResultAllow(
                updated_input=container_session.translate_tool_input(
                    tool_name,
                    tool_input,
                )
            )

        return can_use_tool

    @staticmethod
    def _extract_mcp_servers(
        task_context: dict[str, Any],
    ) -> dict[str, dict[str, Any]] | None:
        servers = task_context.get("mcp_servers")
        if not isinstance(servers, dict) or not servers:
            return None
        return to_sdk_mcp_servers(servers)

    async def _drain_queue(
        self,
        queue: ToolEventQueue,
        sequencer: EventSequencer,
    ) -> AsyncIterator[ThoughtCaptured]:
        while not queue.empty():
            content, output_type, tool_name = queue.get_nowait()
            yield sequencer.thought(content, output_type, tool_name=tool_name)

    async def _process_message(
        self,
        message: Any,
        sequencer: EventSequencer,
    ) -> AsyncIterator[ThoughtCaptured]:
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
        *,
        duration_seconds: float = 0.0,
    ) -> DomainEvent | None:
        cost_usd = getattr(message, "total_cost_usd", None)
        usage = getattr(message, "usage", None) or {}

        # Only emit if we have cost data
        if cost_usd is None and not usage:
            return None

        prompt_tokens = self._usage_int(usage, "input_tokens")
        completion_tokens = self._usage_int(usage, "output_tokens")
        cache_read_tokens = self._usage_int(usage, "cache_read_input_tokens")
        cache_write_tokens = self._usage_int(usage, "cache_creation_input_tokens")
        reasoning_tokens = self._usage_int(usage, "reasoning_tokens") + self._usage_int(
            usage,
            "thinking_tokens",
        )
        # Audit N-3: include cache + reasoning in the total so cross-adapter
        # token comparisons stay symmetric with OpenHands (which already sums
        # all five buckets). The Claude usage block reports cache and
        # reasoning separately; `prompt + completion` alone undercounts.
        total_tokens: int | None = None
        if usage:
            total_tokens = (
                prompt_tokens
                + completion_tokens
                + cache_read_tokens
                + cache_write_tokens
                + reasoning_tokens
            )

        return sequencer.cost_recorded(
            tool_name=self._get_tool_name(),
            cost_usd=cost_usd or 0.0,
            duration_seconds=duration_seconds,
            model=self.config.model,
            tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    @staticmethod
    def _usage_int(usage: Any, key: str) -> int:
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        return int(value or 0)

    def _make_result_event(
        self,
        message: ResultMessage,
        sequencer: EventSequencer,
    ) -> DomainEvent:
        if getattr(message, "is_error", False):
            return sequencer.failed(getattr(message, "result", None) or "Task failed")
        return sequencer.completed(getattr(message, "result", None) or "Task completed")

    @staticmethod
    def _process_block(block: Any) -> tuple[str, str] | None:
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
        error_str = str(error).lower()
        if "cli" in error_str and ("not found" in error_str or "missing" in error_str):
            return (
                "Claude Agent SDK CLI not found. "
                "The CLI is bundled with the SDK - try reinstalling: "
                "pip install --force-reinstall claude-agent-sdk"
            )
        return f"Claude SDK adapter error: {error!r}"
