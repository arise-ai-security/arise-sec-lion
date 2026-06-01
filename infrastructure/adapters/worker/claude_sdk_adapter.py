"""Claude Agent SDK Adapter: uses official SDK for structured tool execution.

Yields ThoughtCaptured events with precise output_type values based on SDK
message types, plus WorkCompleted/WorkFailed on completion.

Uses PostToolUse hooks for capturing tool invocations as events.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
    UsageBreakdown,
    build_claude_container_env,
    emit_cost,
    format_tool_event,
    prepare_claude_container_user,
    to_in_container_mcp_servers,
    to_sdk_mcp_servers,
    write_docker_exec_wrapper,
)


type PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
type ThinkingDisplay = Literal["summarized", "omitted"]
type ToolEvent = tuple[str, str, str | None]
type ToolEventQueue = asyncio.Queue[ToolEvent]
type ToolUseHook = Callable[
    [HookInput, str | None, HookContext],
    Awaitable[SyncHookJSONOutput],
]

DEFAULT_MAX_THINKING_TOKENS = 63_999
CLAUDE_CODE_CLI_PATH_ENV = "CLAUDE_CODE_CLI_PATH"
PROMPT_CACHING_1H_ENV = "ENABLE_PROMPT_CACHING_1H"
IN_CONTAINER_CLAUDE_EXECUTABLE = "claude"
SDK_CONTAINER_WRAPPER_PATH = ".arise/claude-sdk-in-container"


def _builtin_tools_for_loading(allowed_tools: list[str]) -> list[str]:
    # `allowed_tools` is the call-time gate; the CLI's `--tools` flag controls
    # which built-in tool SCHEMAS get loaded into the cached system prefix.
    # Without `--tools`, the CLI ships its full default catalog (32K-55K
    # cache_write tokens/session in current telemetry). Filter to bare
    # built-in names: drop MCP-prefixed entries (loaded via --mcp-config) and
    # permission patterns like `Bash(ls:*)` (allowedTools-only syntax).
    return [
        name
        for name in allowed_tools
        if name and not name.startswith("mcp__") and "(" not in name
    ]


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
    cli_path: str | None = None


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
            await prepare_claude_container_user(container_session)

        try:
            options = self._build_options(
                working_dir,
                tool_queue,
                container_session,
                mcp_servers=self._extract_mcp_servers(
                    task_context,
                    in_container=container_session is not None,
                ),
            )

            async with asyncio.timeout(self.timeout_seconds):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(task_description)

                    async for message in client.receive_response():
                        # Yield pending tool events from hooks
                        async for event in self._drain_queue(
                            tool_queue,
                            sequencer,
                            container_session,
                        ):
                            yield event

                        # Process message content blocks
                        async for event in self._process_message(
                            message,
                            sequencer,
                            container_session,
                        ):
                            yield event

                        # Handle result message (terminal)
                        if isinstance(message, ResultMessage):
                            async for event in self._drain_queue(
                                tool_queue,
                                sequencer,
                                container_session,
                            ):
                                yield event

                            # Emit cost event before terminal event
                            cost_event = self._make_cost_event(
                                message,
                                sequencer,
                                duration_seconds=time() - started_at,
                            )
                            if cost_event:
                                yield cost_event

                            yield self._make_result_event(
                                message,
                                sequencer,
                                container_session,
                            )
                            return

        except TimeoutError:
            yield sequencer.failed(f"Task timed out after {self.timeout_seconds} seconds")

        except Exception as e:
            reason = self._sanitize_for_events(self._format_error(e), container_session)
            yield sequencer.failed(str(reason))

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
            "can_use_tool": None,
            "hooks": {
                "PostToolUse": [
                    HookMatcher(
                        hooks=[
                            self._make_tool_use_hook(
                                tool_queue,
                                container_session,
                            )
                        ]
                    )
                ],
            },
        }
        builtin_tools = _builtin_tools_for_loading(self.config.allowed_tools)
        if builtin_tools:
            kwargs["tools"] = builtin_tools
        if self.config.thinking_display is not None:
            kwargs["extra_args"] = {"thinking-display": self.config.thinking_display}
        if self.config.max_thinking_tokens is not None:
            kwargs["max_thinking_tokens"] = self.config.max_thinking_tokens
        if caching_env := self._prompt_caching_env():
            kwargs["env"] = {**kwargs.get("env", {}), **caching_env}
        if container_session is not None:
            kwargs["cli_path"] = str(self._write_in_container_cli_wrapper(container_session))
        elif cli_path := self._effective_cli_path():
            kwargs["cli_path"] = cli_path
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        return ClaudeAgentOptions(**kwargs)

    @staticmethod
    def _prompt_caching_env() -> dict[str, str]:
        """Forward ENABLE_PROMPT_CACHING_1H into the SDK when set on the host.

        Opts the Claude session into the 1-hour prompt-cache TTL. Host-only and
        opt-in: when unset, no env override is passed and the SDK default (5-min
        TTL) applies. The container path forwards the same var via the
        container_exec allowlist.
        """
        value = os.environ.get(PROMPT_CACHING_1H_ENV)
        return {PROMPT_CACHING_1H_ENV: value} if value else {}

    def _write_in_container_cli_wrapper(
        self,
        container_session: ContainerSessionContext,
    ) -> Path:
        return write_docker_exec_wrapper(
            path=(container_session.workspace_root / SDK_CONTAINER_WRAPPER_PATH).resolve(),
            container_session=container_session,
            executable=IN_CONTAINER_CLAUDE_EXECUTABLE,
            env=build_claude_container_env(scratch_container_dir=None),
        )

    def _effective_cli_path(self) -> str | None:
        cli_path = self.config.cli_path or os.getenv(CLAUDE_CODE_CLI_PATH_ENV)
        if cli_path is None:
            return None
        stripped = cli_path.strip()
        return stripped or None

    def _make_tool_use_hook(
        self,
        tool_queue: ToolEventQueue,
        container_session: ContainerSessionContext | None,
    ) -> ToolUseHook:
        async def capture_tool_use(
            input_data: HookInput,
            _tool_use_id: str | None,
            _context: HookContext,
        ) -> SyncHookJSONOutput:
            tool_name, tool_input = self._extract_hook_tool_call(input_data)
            sanitized_input = self._sanitize_for_events(
                tool_input,
                container_session,
            )
            if not isinstance(sanitized_input, dict):
                sanitized_input = tool_input
            # Mirror claude_code_worker.py:565-567 and openhands_adapter.py:692:
            # append `\nInput: {<json>}` so downstream parsers that expect the
            # JSON-encoded tool input alongside the description (e.g.,
            # bash-command recovery) can find it.
            detail = json.dumps(sanitized_input, sort_keys=True, default=str)
            description = format_tool_event(tool_name, sanitized_input)
            content = f"{description}\nInput: {detail}"
            # Audit N-6: pass the canonical tool_name through so the
            # downstream ThoughtCaptured event has a structured field
            # instead of forcing offline metrics to parse the human prefix.
            await tool_queue.put((content, "tool_use", tool_name))
            return SyncHookJSONOutput()

        return capture_tool_use

    @staticmethod
    def _extract_hook_tool_call(input_data: HookInput) -> tuple[str, dict[str, Any]]:
        if isinstance(input_data, Mapping):
            raw_name = input_data.get("tool_name", "unknown")
            raw_input = input_data.get("tool_input", {})
        else:
            raw_name = getattr(input_data, "tool_name", "unknown")
            raw_input = getattr(input_data, "tool_input", {})
        tool_name = raw_name if isinstance(raw_name, str) else "unknown"
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        return tool_name, tool_input

    @staticmethod
    def _extract_mcp_servers(
        task_context: dict[str, Any],
        *,
        in_container: bool = False,
    ) -> dict[str, dict[str, Any]] | None:
        servers = task_context.get("mcp_servers")
        if not isinstance(servers, dict) or not servers:
            return None
        if in_container:
            servers = to_in_container_mcp_servers(servers)
        return to_sdk_mcp_servers(servers)

    async def _drain_queue(
        self,
        queue: ToolEventQueue,
        sequencer: EventSequencer,
        container_session: ContainerSessionContext | None = None,
    ) -> AsyncIterator[ThoughtCaptured]:
        while not queue.empty():
            content, output_type, tool_name = queue.get_nowait()
            content = str(self._sanitize_for_events(content, container_session))
            yield sequencer.thought(content, output_type, tool_name=tool_name)

    async def _process_message(
        self,
        message: Any,
        sequencer: EventSequencer,
        container_session: ContainerSessionContext | None = None,
    ) -> AsyncIterator[ThoughtCaptured]:
        if not isinstance(message, AssistantMessage):
            return

        for block in message.content:
            result = self._process_block(block, container_session)
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

        if usage:
            breakdown = UsageBreakdown(
                prompt_tokens=self._usage_int(usage, "input_tokens"),
                completion_tokens=self._usage_int(usage, "output_tokens"),
                cache_read_tokens=self._usage_int(usage, "cache_read_input_tokens"),
                cache_write_tokens=self._usage_int(usage, "cache_creation_input_tokens"),
                reasoning_tokens=(
                    self._usage_int(usage, "reasoning_tokens")
                    + self._usage_int(usage, "thinking_tokens")
                ),
                cost_usd=cost_usd,
            )
        else:
            breakdown = UsageBreakdown(cost_usd=cost_usd)

        return emit_cost(
            sequencer,
            tool_name=self._get_tool_name(),
            duration_seconds=duration_seconds,
            model=self.config.model,
            breakdown=breakdown,
        )

    @staticmethod
    def _usage_int(usage: Any, key: str) -> int:
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        return int(value or 0)

    def _make_result_event(
        self,
        message: ResultMessage,
        sequencer: EventSequencer,
        container_session: ContainerSessionContext | None = None,
    ) -> DomainEvent:
        result = self._sanitize_for_events(
            getattr(message, "result", None),
            container_session,
        )
        if getattr(message, "is_error", False):
            return sequencer.failed(str(result or "Task failed"))
        return sequencer.completed(str(result or "Task completed"))

    @staticmethod
    def _sanitize_for_events(
        value: Any,
        container_session: ContainerSessionContext | None,
    ) -> Any:
        if container_session is None:
            return value
        return container_session.path_mapper.sanitize_host_paths(value)

    @classmethod
    def _process_block(
        cls,
        block: Any,
        container_session: ContainerSessionContext | None = None,
    ) -> tuple[str, str] | None:
        if isinstance(block, TextBlock):
            text = cls._sanitize_for_events(block.text, container_session)
            if isinstance(text, str) and text.strip():
                return text, "output"
        elif isinstance(block, ThinkingBlock):
            thinking = cls._sanitize_for_events(block.thinking, container_session)
            if isinstance(thinking, str) and thinking.strip():
                return thinking, "thinking"
        elif isinstance(block, ToolResultBlock):
            sanitized = cls._sanitize_for_events(block.content, container_session)
            content = str(sanitized)[:500] if sanitized else "(no output)"
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

    def _format_unexpected_error(self, error: Exception) -> str:
        return self._format_error(error)
