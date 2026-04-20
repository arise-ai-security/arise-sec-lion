"""Claude Agent SDK Adapter: uses official SDK for structured tool execution.

Yields ThoughtCaptured events with precise output_type values based on SDK
message types, plus WorkCompleted/WorkFailed on completion.

Uses paired PreToolUse + PostToolUse hooks so each tool invocation event is
annotated with the ``tool_use_id`` (``call_id``) and the per-call
``duration_ms`` derived from the monotonic PreToolUse start timestamp.
"""

import asyncio
import logging
import time
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
    ToolUseBlock,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from core.domain.events.events import DomainEvent, ThoughtCaptured

from .base import WorkerAdapterBase
from .shared import ContainerSessionContext, EventSequencer, format_tool_event
from .shared.truncation import cap_thought_content


logger = logging.getLogger(__name__)


type PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]

# Queue tuple shape: (formatted_content, output_type, tool_use_id, tool_input_json).
# Keeping ``tool_input_json`` on the queue lets the single _drain_queue site
# build ThoughtCaptured with full metadata (call_id + duration_ms + structured
# input) instead of forcing each producer to duplicate the kwargs plumbing.
type _ToolEventEntry = tuple[str, str, str | None, dict[str, Any] | None]


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
        # Tracks tool_use_id -> start timestamp for per-call duration computation.
        # Safe across concurrent SDK sessions because: (1) Anthropic's tool_use_id is
        # globally unique per API request, and (2) dict insert/pop are atomic under the GIL.
        # Cleared at the start of each _execute_task to prevent accumulation when
        # PreToolUse fires but PostToolUse doesn't (SDK error / timeout).
        self._pending_tool_starts: dict[str, float] = {}
        super().__init__(timeout_seconds=self.config.timeout_seconds)

    def _pre_tool_use_hook(self, tool_use_id: str) -> None:
        """Record a start timestamp for pairing with the PostToolUse hook."""
        self._pending_tool_starts[tool_use_id] = time.monotonic()

    def _reset_session_state(self) -> None:
        """Clear transient state carried between SDK sessions.

        The adapter is a long-lived singleton; any PreToolUse start time whose
        matching PostToolUse didn't fire (SDK error / timeout) would otherwise
        leak into the next session's dict. Resetting at session start keeps
        ``_pending_tool_starts`` bounded.
        """
        self._pending_tool_starts.clear()

    def _get_and_pop_duration_ms(self, tool_use_id: str | None) -> int | None:
        """Pop the PreToolUse start for ``tool_use_id`` and return duration in ms.

        Returns None if no matching PreToolUse was recorded (e.g., the SDK
        did not emit one, or a restart dropped the in-memory state). Never
        raises on unknown ids. Emits DEBUG for known-non-None ids that miss so
        dataset holes are observable without spamming WARNING for internal
        tool types that may legitimately skip PreToolUse.
        """
        if tool_use_id is None:
            return None
        start = self._pending_tool_starts.pop(tool_use_id, None)
        if start is None:
            logger.debug(
                "PostToolUse for unknown tool_use_id: %s (PreToolUse may not have fired)",
                tool_use_id,
            )
            return None
        return int((time.monotonic() - start) * 1000)

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

        Yields events as they are produced, not after completion. Pairs
        PreToolUse + PostToolUse hooks to capture per-tool-call duration.
        """
        self._reset_session_state()
        self._start_timing()
        tool_queue: asyncio.Queue[_ToolEventEntry] = asyncio.Queue()
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
        tool_queue: asyncio.Queue[_ToolEventEntry],
        container_session: ContainerSessionContext | None,
        runtime_model: str | None,
    ) -> ClaudeAgentOptions:
        """Build SDK options with Pre/PostToolUse hooks for event + timing capture."""

        async def record_tool_start(
            _input_data: HookInput,
            tool_use_id: str | None,
            _context: HookContext,
        ) -> SyncHookJSONOutput:
            """PreToolUse: stash a start timestamp keyed by tool_use_id."""
            if tool_use_id is not None:
                self._pre_tool_use_hook(tool_use_id)
            return SyncHookJSONOutput()

        async def capture_tool_use(
            input_data: HookInput,
            tool_use_id: str | None,
            _context: HookContext,
        ) -> SyncHookJSONOutput:
            """PostToolUse: capture tool invocation + structured input downstream."""
            tool_name = getattr(input_data, "tool_name", "unknown")
            tool_input = getattr(input_data, "tool_input", {}) or {}
            content = format_tool_event(tool_name, tool_input)
            tool_input_json = dict(tool_input) if isinstance(tool_input, dict) else None
            await tool_queue.put((content, "tool_use", tool_use_id, tool_input_json))
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
            # Pin the system prompt explicitly so Anthropic's prefix cache
            # can hit across sibling worker sessions. See
            # docs/pillar_b/scripts/worker_cache_audit.py for the v1 audit.
            system_prompt={"type": "preset", "preset": "claude_code"},
            hooks={
                "PreToolUse": [HookMatcher(hooks=[record_tool_start])],
                "PostToolUse": [HookMatcher(hooks=[capture_tool_use])],
            },
        )

    async def _drain_queue(
        self,
        queue: asyncio.Queue[_ToolEventEntry],
        sequencer: EventSequencer,
    ) -> AsyncIterator[ThoughtCaptured]:
        """Drain all pending events from queue, attaching per-call duration_ms."""
        while not queue.empty():
            content, output_type, tool_use_id, tool_input_json = queue.get_nowait()
            duration_ms = self._get_and_pop_duration_ms(tool_use_id)
            yield sequencer.thought(
                content,
                output_type,
                call_id=tool_use_id,
                duration_ms=duration_ms,
                tool_input_json=tool_input_json,
            )

    async def _process_message(
        self,
        message: Any,
        sequencer: EventSequencer,
    ) -> AsyncIterator[ThoughtCaptured]:
        """Process SDK message and yield domain events.

        ToolUseBlock in AssistantMessage content is skipped at runtime: the
        PostToolUse hook (``capture_tool_use``) owns ``tool_use`` emission and
        carries the paired ``duration_ms`` from PreToolUse. Routing both through
        the hook keeps one-to-one event semantics per tool invocation. The pure
        ``_process_block`` helper still handles ``ToolUseBlock`` for testability
        and for any future block-only path (e.g., replay without hooks).

        ``ToolResultBlock`` events carry ``duration_ms=None`` by design: the
        timing was already emitted on the matching ``tool_use`` event (see the
        comment inside the loop). Pillar B analysis should join ``tool_use``
        and ``tool_result`` by ``call_id`` and read ``duration_ms`` from the
        ``tool_use`` event.
        """
        if not isinstance(message, AssistantMessage):
            return

        for block in message.content:
            if isinstance(block, ToolUseBlock):
                continue  # Hook path owns tool_use emission.
            result = self._process_block(block)
            if result is None:
                continue
            content, output_type, metadata = result
            call_id = metadata.get("call_id")
            # Note: duration_ms for this tool call was already emitted on the
            # matching 'tool_use' event (populated via _drain_queue's pop of the
            # PreToolUse start timestamp). We do NOT call
            # _get_and_pop_duration_ms here because the entry has been consumed.
            # Pillar B analysis should read duration_ms from
            # output_type='tool_use' events, not output_type='tool_result'.
            yield sequencer.thought(
                content,
                output_type,
                call_id=call_id,
                duration_ms=None,
                was_truncated=metadata.get("was_truncated", False),
                result_bytes=metadata.get("result_bytes"),
                tool_input_json=metadata.get("tool_input_json"),
            )

    def _make_cost_event(
        self,
        message: ResultMessage,
        sequencer: EventSequencer,
        runtime_model: str | None,
    ) -> DomainEvent | None:
        """Create a WorkerCostRecorded event from a Claude Agent SDK ResultMessage.

        Captures the full Anthropic usage breakdown (input, output, cache_read,
        cache_creation) so downstream cost projections can distinguish cached
        from fresh tokens (Anthropic prices cached reads at ~10% of fresh input
        tokens). Also forwards ``reasoning_tokens`` if the upstream usage dict
        supplies it; the current Anthropic API folds extended-thinking output
        into ``output_tokens``, but keeping the field wired matches the shape of
        other adapters and is future-proof against an API change.
        """
        cost_usd = getattr(message, "total_cost_usd", None)
        usage = getattr(message, "usage", None) or {}

        # No usage dict and no cost -> nothing meaningful to record.
        if cost_usd is None and not usage:
            return None

        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        cache_read_tokens = usage.get("cache_read_input_tokens")
        cache_write_tokens = usage.get("cache_creation_input_tokens")
        reasoning_tokens = usage.get("reasoning_tokens")

        # Total only counts dimensions we actually have data for; if the usage
        # dict was empty, total_tokens stays None so it matches the "no data" signal.
        parts = [
            t
            for t in (
                prompt_tokens,
                completion_tokens,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
            )
            if t is not None
        ]
        total_tokens = sum(parts) if parts else None

        return sequencer.cost_recorded(
            tool_name=self._get_tool_name(),
            cost_usd=cost_usd or 0.0,
            duration_seconds=self._get_duration(),
            model=runtime_model,
            tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
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
    def _process_block(
        block: Any,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Process a single content block, returning ``(content, output_type, metadata)``.

        ``metadata`` carries the schema-parity fields for ``ThoughtCaptured``:
        ``call_id``, ``was_truncated``, ``result_bytes``, ``tool_input_json``.
        Only keys relevant for the block type are populated; the caller forwards
        them verbatim to ``EventSequencer.thought``.
        """
        if isinstance(block, TextBlock):
            if block.text.strip():
                return block.text, "output", {}
        elif isinstance(block, ThinkingBlock):
            if block.thinking.strip():
                return block.thinking, "thinking", {}
        elif isinstance(block, ToolUseBlock):
            tool_input = getattr(block, "input", {}) or {}
            formatted = format_tool_event(getattr(block, "name", "unknown"), tool_input)
            return (
                formatted,
                "tool_use",
                {
                    "call_id": getattr(block, "id", None),
                    "tool_input_json": (
                        dict(tool_input) if isinstance(tool_input, dict) else None
                    ),
                },
            )
        elif isinstance(block, ToolResultBlock):
            raw_content = str(block.content) if block.content else "(no output)"
            capped, was_truncated, result_bytes = cap_thought_content(raw_content)
            return (
                f"Tool result: {capped}",
                "tool_result",
                {
                    "call_id": getattr(block, "tool_use_id", None),
                    "was_truncated": was_truncated,
                    "result_bytes": result_bytes,
                },
            )
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
