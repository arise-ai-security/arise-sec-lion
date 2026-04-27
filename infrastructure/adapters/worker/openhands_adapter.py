"""OpenHands SDK adapter: use the SDK conversation directly for task execution."""

import asyncio
import concurrent.futures
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import UUID

from core.domain.events.events import (
    DomainEvent,
    WorkerCostItem,
    WorkerResponseLatencyItem,
    WorkerTokenUsageItem,
    WorkerUsageMetrics,
)

from .base import WorkerAdapterBase
from .shared import ContainerSessionContext, EventSequencer


# Suppress verbose OpenHands logging
logging.getLogger("openhands").setLevel(logging.WARNING)
logging.getLogger("openhands.sdk").setLevel(logging.WARNING)
logging.getLogger("openhands.tools").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS_PER_RUN = 20
_SHUTDOWN_GRACE_SECONDS = 5


class _ConversationLike(Protocol):
    """OpenHands SDK conversation surface used by this adapter."""

    def send_message(self, message: str) -> None: ...

    def run(self) -> object: ...


def _patch_openhands_fn_converter() -> None:
    """Tolerate assistant messages that lost their 'content' key.

    Why: Qwen via Ollama frequently emits a function call with no preceding
    prose. OpenHands' prompt-mocked converter strips the <function=...> tag,
    leaving content="". On the next turn, Message.to_chat_dict calls
    _remove_content_if_empty which drops the content key for assistant
    messages with tool_calls. The next pass through
    convert_fncall_messages_to_non_fncall_messages then crashes with
    KeyError('content') at fn_call_converter.py:705 — surfaced as
    ConversationRunError("...: 'content'"). Claude/GPT don't trigger this
    because they always include reasoning text before the tag.
    """
    from openhands.sdk.llm.mixins import fn_call_converter as _fcc, non_native_fc as _nnfc

    if getattr(_fcc, "_arise_content_key_patched", False):
        return
    _orig = _fcc.convert_fncall_messages_to_non_fncall_messages

    def _patched(
        messages: list[dict[str, Any]],
        tools: list[Any],
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        for msg in messages:
            if "content" not in msg:
                msg["content"] = ""
        return _orig(messages, tools, *args, **kwargs)

    # Patch the canonical definition in fn_call_converter
    _fcc.convert_fncall_messages_to_non_fncall_messages = _patched
    # Also patch the reference imported by non_native_fc, which captures the
    # original function at import time and bypasses the module-level patch.
    converter_name = "convert_fncall_messages_to_non_fncall_messages"
    setattr(_nnfc, converter_name, _patched)
    _fcc._arise_content_key_patched = True  # type: ignore[attr-defined]


SDK_EVENT_TYPE_MAP: dict[str, str] = {
    "ActionEvent": "tool_use",
    "AgentThinkAction": "thinking",
    "MessageEvent": "output",
    "ObservationEvent": "tool_result",
    "CmdRunObservation": "tool_result",
    "AgentErrorEvent": "output",
    "FileReadAction": "tool_use",
    "FileWriteAction": "tool_use",
    "FileEditAction": "tool_use",
}


@dataclass(slots=True)
class OpenHandsCostData:
    """Normalized OpenHands cost data used to build WorkerCostRecorded events."""

    cost_usd: float = 0.0
    tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_metrics: list[WorkerUsageMetrics] = field(default_factory=list)


class OpenHandsAdapter(WorkerAdapterBase):
    """Execute tasks via the OpenHands SDK with native conversation controls."""

    STREAM_NAME = "openhands"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 300,
        max_iterations_per_run: int = DEFAULT_MAX_ITERATIONS_PER_RUN,
        base_url: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> None:
        """Initialize OpenHands adapter."""
        super().__init__(timeout_seconds=timeout_seconds)
        self.model: str = model or os.getenv("LLM_MODEL") or "openai/gpt-4o"
        self.api_key = api_key or self._detect_api_key()
        self.max_iterations_per_run: int = max_iterations_per_run
        # None lets OpenHands/LiteLLM fall back to env vars (e.g. OLLAMA_API_BASE).
        self.base_url: str | None = base_url
        self.allowed_tools: list[str] = allowed_tools or ["*"]
        self.disallowed_tools: list[str] = disallowed_tools or []

    def _detect_api_key(self) -> str | None:
        """Detect appropriate API key based on model name."""
        if api_key := os.getenv("LLM_API_KEY"):
            return api_key

        model_lower = self.model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return os.getenv("ANTHROPIC_API_KEY")
        if "gemini" in model_lower or "google" in model_lower:
            return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if model_lower.startswith(("ollama/", "ollama_chat/")) or "ollama" in model_lower:
            return os.getenv("OLLAMA_API_KEY")
        return os.getenv("OPENAI_API_KEY")

    def _get_tool_name(self) -> str:
        """Return tool identifier for cost tracking."""
        return "openhands"

    async def _execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        """Execute task via OpenHands SDK with native conversation lifecycle."""
        del agent_id
        self._start_timing()
        container_session = ContainerSessionContext.from_task_context(task_context)
        if container_session is not None:
            task_description = container_session.apply_task_prefix(
                task_description,
                auto_shell=False,
            )

        try:
            conversation = cast("_ConversationLike", self._build_conversation(working_dir))
        except ImportError as error:
            yield sequencer.failed(
                "OpenHands packages not installed. "
                f"Install with: uv add openhands-sdk openhands-tools. Error: {error}"
            )
            return
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {error!r}")
            return

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="openhands"
        )
        try:
            conversation.send_message(task_description)  # pylint: disable=no-member
            future = executor.submit(conversation.run)  # pylint: disable=no-member

            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(future),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError:
                self._request_shutdown(conversation)
                await self._await_thread_exit(future)
                yield sequencer.failed(
                    f"Task timed out after {self.timeout_seconds} seconds"
                )
                return

            finish_message: str | None = None
            for event in self._iter_conversation_events(conversation):
                content = self._extract_event_content(event)
                if content and content.strip():
                    yield sequencer.thought(
                        content.strip(),
                        self._classify_event(event),
                    )
                # Capture finish message during iteration
                if finish_message is None:
                    finish_message = self._try_extract_finish(event)

            cost_data = self._extract_cost_data(conversation)
            yield sequencer.cost_recorded(
                tool_name=self._get_tool_name(),
                cost_usd=cost_data.cost_usd,
                duration_seconds=self._get_duration(),
                model=self._resolve_cost_model(cost_data),
                tokens=cost_data.tokens,
                prompt_tokens=cost_data.prompt_tokens,
                completion_tokens=cost_data.completion_tokens,
                cache_read_tokens=cost_data.cache_read_tokens,
                cache_write_tokens=cost_data.cache_write_tokens,
                reasoning_tokens=cost_data.reasoning_tokens,
                usage_metrics=cost_data.usage_metrics,
            )

            yield sequencer.completed(
                self._extract_result(conversation, working_dir, finish_message)
            )
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {error!r}")
        finally:
            self._request_shutdown(conversation)
            executor.shutdown(wait=False)

    def _build_conversation(self, working_dir: str) -> Any:
        """Create an OpenHands SDK conversation for the current task."""
        from openhands.sdk import LLM, Agent, Conversation, Tool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.terminal import TerminalTool

        _patch_openhands_fn_converter()

        llm_kwargs: dict[str, Any] = {"model": self.model, "api_key": self.api_key}
        if self.base_url:
            llm_kwargs["base_url"] = self.base_url
        # Qwen-family models via Ollama report context_length (e.g. 262144)
        # as max_output_tokens, but the actual output limit is lower.
        # litellm's auto-detection conflates context window with output cap,
        # causing Ollama to reject the request.  Cap explicitly.
        # Ollama Cloud enforces a 65536 server-side output limit for
        # qwen3.5:397b-cloud; use 65000 to stay within bounds while giving
        # workers ample budget for thinking + multi-turn tool calls.
        if self.model.startswith(("ollama/", "ollama_chat/")):
            llm_kwargs["max_output_tokens"] = 65000
            # litellm marks Ollama models as supports_function_calling=False,
            # triggering a crude single-shot JSON fallback that forces
            # `format: json` and a "Produce JSON OUTPUT ONLY!" prompt.
            # This is fundamentally incompatible with OpenHands' multi-turn
            # agent loop: the model generates minimal JSON, never executes
            # any commands, and finishes immediately (0 ThoughtCaptured).
            # Setting native_tool_calling=False makes OpenHands use its own
            # XML-based prompt mocking (<function=...> / </function>) which
            # works correctly with multi-turn conversations and does not
            # force format constraints.
            llm_kwargs["native_tool_calling"] = False
            # Disable thinking/reasoning mode for Ollama reasoning models.
            # qwen3.5 wraps output in inline <think> tags; deepseek-v4-flash
            # emits a separate `thinking` field — both routinely consume
            # the response budget on reasoning and leave `content` empty
            # or malformed (missing tool-call args, truncated JSON).
            # Ollama exposes a top-level `think: false` flag that
            # suppresses both behaviours; the older nested
            # `options.think` form is silently ignored by deepseek and
            # recent Ollama Cloud builds. litellm's `reasoning_effort`
            # param does NOT propagate to Ollama — must use extra_body.
            llm_kwargs["litellm_extra_body"] = {"think": False}
        llm = LLM(**llm_kwargs)
        tools = []
        if self._tool_allowed("Bash", "Terminal", "execute_command", "shell_execute"):
            # Force subprocess-backed shell instead of the auto-detected tmux
            # PTY: tmux streams commands character-by-character with
            # bracketed-paste toggling per line, which stalls on complex
            # nested quoting (e.g. ./run-cmd "... grep 'id=\"[^\"]*\"' ...").
            # Subprocess sends the command as argv, bypassing terminal
            # emulation entirely.
            tools.append(
                Tool(
                    name=TerminalTool.name,
                    params={"terminal_type": "subprocess"},
                )
            )
        if self._file_editor_allowed():
            tools.append(Tool(name=FileEditorTool.name))

        agent = Agent(llm=llm, tools=tools)
        return Conversation(
            agent=agent,
            workspace=working_dir,
            max_iteration_per_run=self.max_iterations_per_run,
        )

    def _tool_allowed(self, *names: str) -> bool:
        """Return whether any of ``names`` survives the global policy."""
        blocked = set(self.disallowed_tools)
        if any(name in blocked for name in names):
            return False
        if self.allowed_tools == ["*"]:
            return True
        allowed = set(self.allowed_tools)
        return any(name in allowed for name in names)

    def _file_editor_allowed(self) -> bool:
        """Map global file-tool policy to OpenHands' coarse FileEditorTool.

        OpenHands exposes read/write/edit through one SDK tool. If an
        experiment blocks any mutating file operation, we disable the entire
        file-editor surface rather than silently allowing writes.
        """
        mutating_aliases = {
            "Write",
            "Edit",
            "MultiEdit",
            "NotebookEdit",
            "write_file",
            "create_directory",
            "file_editor",
        }
        if mutating_aliases & set(self.disallowed_tools):
            return False
        return self._tool_allowed("Read", "Write", "Edit", "file_editor", "read_file")

    def _iter_conversation_events(self, conversation: Any) -> list[Any]:
        """Return the SDK events recorded for this conversation."""
        state = getattr(conversation, "state", None)
        if state is None or not hasattr(state, "events"):
            return []
        return list(state.events)

    def _extract_cost_data(self, conversation: Any) -> OpenHandsCostData:
        """Extract cost and token detail from conversation_stats."""
        stats = getattr(conversation, "conversation_stats", None)
        if stats is None:
            return OpenHandsCostData()

        usage_to_metrics = self._get_stat(stats, "usage_to_metrics")
        return (
            self._extract_usage_metrics(usage_to_metrics)
            if usage_to_metrics
            else OpenHandsCostData()
        )

    def _extract_usage_metrics(self, usage_to_metrics: Any) -> OpenHandsCostData:
        """Extract detailed metrics from ConversationStats.usage_to_metrics."""
        items = usage_to_metrics.items() if isinstance(usage_to_metrics, dict) else []
        usage_metrics: list[WorkerUsageMetrics] = []
        total_cost = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0

        for usage_id, metrics in items:
            usage_metric = self._build_usage_metrics(str(usage_id), metrics)
            usage_metrics.append(usage_metric)
            total_cost += usage_metric.accumulated_cost_usd
            prompt_tokens += usage_metric.prompt_tokens
            completion_tokens += usage_metric.completion_tokens
            cache_read_tokens += usage_metric.cache_read_tokens
            cache_write_tokens += usage_metric.cache_write_tokens
            reasoning_tokens += usage_metric.reasoning_tokens

        if not usage_metrics:
            return OpenHandsCostData()

        return OpenHandsCostData(
            cost_usd=total_cost,
            tokens=self._sum_tokens(
                prompt_tokens,
                completion_tokens,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
            ),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_metrics=usage_metrics,
        )

    def _build_usage_metrics(self, usage_id: str, metrics: Any) -> WorkerUsageMetrics:
        """Normalize one Metrics or MetricsSnapshot record into event payload data."""
        model = self._get_stat(metrics, "model_name")
        accumulated_cost = float(self._get_stat(metrics, "accumulated_cost", 0.0) or 0.0)
        (
            prompt_tokens,
            completion_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
        ) = self._extract_accumulated_token_usage(metrics)

        return WorkerUsageMetrics(
            usage_id=usage_id,
            model=model,
            accumulated_cost_usd=accumulated_cost,
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            cache_read_tokens=cache_read_tokens or 0,
            cache_write_tokens=cache_write_tokens or 0,
            reasoning_tokens=reasoning_tokens or 0,
            cost_items=[
                WorkerCostItem(
                    model=self._get_stat(cost, "model", "") or model or "",
                    cost_usd=float(
                        self._get_stat(cost, "cost_usd", self._get_stat(cost, "cost", 0.0))
                        or 0.0
                    ),
                    timestamp=self._maybe_float(self._get_stat(cost, "timestamp")),
                )
                for cost in self._get_stat(metrics, "costs", []) or []
            ],
            response_latencies=[
                WorkerResponseLatencyItem(
                    model=self._get_stat(latency, "model", "") or model or "",
                    latency_seconds=float(self._get_stat(latency, "latency", 0.0) or 0.0),
                    response_id=str(self._get_stat(latency, "response_id", "") or ""),
                )
                for latency in self._get_stat(metrics, "response_latencies", []) or []
            ],
            token_usages=[
                WorkerTokenUsageItem(
                    model=self._get_stat(token_usage, "model", "") or model or "",
                    prompt_tokens=int(self._get_stat(token_usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(
                        self._get_stat(token_usage, "completion_tokens", 0) or 0
                    ),
                    cache_read_tokens=int(
                        self._get_stat(token_usage, "cache_read_tokens", 0) or 0
                    ),
                    cache_write_tokens=int(
                        self._get_stat(token_usage, "cache_write_tokens", 0) or 0
                    ),
                    reasoning_tokens=int(
                        self._get_stat(token_usage, "reasoning_tokens", 0) or 0
                    ),
                    context_window=int(self._get_stat(token_usage, "context_window", 0) or 0),
                    per_turn_token=int(self._get_stat(token_usage, "per_turn_token", 0) or 0),
                    response_id=str(self._get_stat(token_usage, "response_id", "") or ""),
                )
                for token_usage in self._get_stat(metrics, "token_usages", []) or []
            ],
        )

    def _extract_accumulated_token_usage(
        self, stats: Any,
    ) -> tuple[int | None, int | None, int | None, int | None, int | None]:
        """Extract aggregate token categories from metrics or legacy dicts."""
        accumulated = self._get_stat(stats, "accumulated_token_usage")
        if accumulated is not None:
            return (
                self._maybe_int(self._get_stat(accumulated, "prompt_tokens")),
                self._maybe_int(self._get_stat(accumulated, "completion_tokens")),
                self._maybe_int(self._get_stat(accumulated, "cache_read_tokens")),
                self._maybe_int(self._get_stat(accumulated, "cache_write_tokens")),
                self._maybe_int(self._get_stat(accumulated, "reasoning_tokens")),
            )

        return (
            self._maybe_int(self._get_stat(stats, "prompt_tokens")),
            self._maybe_int(self._get_stat(stats, "completion_tokens")),
            self._maybe_int(self._get_stat(stats, "cache_read_tokens")),
            self._maybe_int(self._get_stat(stats, "cache_write_tokens")),
            self._maybe_int(self._get_stat(stats, "reasoning_tokens")),
        )

    def _resolve_cost_model(self, cost_data: OpenHandsCostData) -> str | None:
        """Return a representative model value for the aggregate worker event."""
        usage_models = {usage.model for usage in cost_data.usage_metrics if usage.model}
        if len(usage_models) == 1:
            return next(iter(usage_models))
        return self.model

    @staticmethod
    def _get_stat(obj: Any, name: str, default: Any = None) -> Any:
        """Read an attribute or dict key without assuming an SDK object shape."""
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        """Normalize optional numeric values to integers."""
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _maybe_float(value: Any) -> float | None:
        """Normalize optional numeric values to floats."""
        if value is None:
            return None
        return float(value)

    @staticmethod
    def _sum_tokens(*parts: int | None) -> int | None:
        """Sum optional token counts, returning None when no data exists."""
        if not any(part is not None for part in parts):
            return None
        return sum(part or 0 for part in parts)

    _EVENT_TEXT_LIMIT = 600

    def _extract_result(
        self, conversation: Any, working_dir: str, finish_message: str | None = None,
    ) -> str:
        """Build a compact command log from ALL conversation events.

        Instead of dumping raw SDK repr for a few events, this creates a
        structured ``$ command (exit N)`` + truncated output summary for
        every output event. The verification judge can then see the full
        execution timeline at a glance.
        """
        entries: list[str] = []
        for event in self._iter_conversation_events(conversation):
            if self._classify_event(event) not in {"output", "tool_result"}:
                continue
            summary = self._compact_observation(event)
            if summary:
                entries.append(summary)

        parts: list[str] = []
        if entries:
            parts.extend(entries)
        if finish_message:
            parts.append(f"--- Agent Conclusion ---\n{finish_message}")
        if parts:
            return "\n".join(parts)
        return f"Task completed in workspace: {working_dir}"

    @classmethod
    def _compact_observation(cls, event: Any) -> str | None:
        """Extract command, exit code, and truncated text from an SDK event."""
        obs = getattr(event, "observation", None)
        if obs is None:
            raw = cls._extract_event_content(event)
            if not raw:
                return None
            return cls._truncate_text(raw, cls._EVENT_TEXT_LIMIT)

        cmd = getattr(obs, "command", "") or ""
        metadata = getattr(obs, "metadata", None)
        exit_code = getattr(metadata, "exit_code", None) if metadata else None
        text = cls._observation_text(obs)

        header = f"$ {cmd} (exit {exit_code})" if cmd else ""
        if text:
            text = cls._truncate_text(text, cls._EVENT_TEXT_LIMIT)

        if header and text:
            return f"{header}\n{text}"
        return header or text or None

    @staticmethod
    def _observation_text(obs: Any) -> str:
        """Extract the plain text payload from an observation object."""
        content = getattr(obs, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    return str(text)
        return str(content)

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        """Head+tail truncation of a single text block."""
        if len(text) <= limit:
            return text
        half = limit // 2
        return (
            f"{text[:half]}\n"
            f"[...{len(text) - limit} chars truncated...]\n"
            f"{text[-half:]}"
        )

    @staticmethod
    def _try_extract_finish(event: Any) -> str | None:
        """Try to extract a FinishAction message from an SDK event."""
        action = getattr(event, "action", None)
        if action is None:
            return None
        if type(action).__name__ != "FinishAction":
            return None
        msg = getattr(action, "message", None)
        return str(msg).strip() if msg and str(msg).strip() else None

    def _request_shutdown(self, conversation: Any) -> None:
        """Best-effort SDK-native cleanup for the conversation."""
        for method_name in ("pause", "close"):
            method = getattr(conversation, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    logger.debug(
                        "OpenHands conversation %s() failed during cleanup",
                        method_name,
                        exc_info=True,
                    )

    async def _await_thread_exit(
        self, future: concurrent.futures.Future,  # type: ignore[type-arg]
    ) -> None:
        """Give the background thread a grace period to exit after shutdown."""
        if future.done():
            return
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=_SHUTDOWN_GRACE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "OpenHands thread did not exit within %ds after shutdown; "
                "it will run until max_iteration_per_run (%d) is reached",
                _SHUTDOWN_GRACE_SECONDS,
                self.max_iterations_per_run,
            )
        except Exception:
            logger.debug("OpenHands thread exit wait failed", exc_info=True)

    @staticmethod
    def _classify_event(event: Any) -> str:
        """Classify SDK event type to output_type."""
        if getattr(event, "action", None) is not None:
            return "tool_use"
        if getattr(event, "observation", None) is not None:
            return "tool_result"
        event_class_name = type(event).__name__
        return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")

    @classmethod
    def _extract_event_content(cls, event: Any) -> str | None:
        """Extract content from SDK event."""
        if hasattr(event, "action") and event.action:
            return cls._format_action_event(event.action)
        if hasattr(event, "observation") and event.observation:
            return cls._format_observation_event(event.observation)
        if hasattr(event, "message") and event.message:
            return str(event.message)
        if hasattr(event, "content") and event.content:
            return str(event.content)
        if hasattr(event, "thought") and event.thought:
            return f"Thought: {event.thought}"
        return None

    @classmethod
    def _format_action_event(cls, action: Any) -> str:
        """Format an OpenHands action as a canonical tool_use event."""
        tool_name = type(action).__name__ or "OpenHandsAction"
        payload: dict[str, Any] = {}
        for attr_name in ("command", "path", "file_path", "thought", "content"):
            value = getattr(action, attr_name, None)
            if value:
                payload[attr_name] = str(value)
        detail = (
            payload
            if payload
            else {"repr": cls._truncate_text(str(action), cls._EVENT_TEXT_LIMIT)}
        )
        return (
            f"Tool: {tool_name}\n"
            f"Input: {json_dumps_for_event(detail)}"
        )

    @classmethod
    def _format_observation_event(cls, observation: Any) -> str:
        """Format an OpenHands observation as a canonical tool_result event."""
        command = getattr(observation, "command", "") or ""
        metadata = getattr(observation, "metadata", None)
        exit_code = getattr(metadata, "exit_code", None) if metadata else None
        text = cls._truncate_text(cls._observation_text(observation), cls._EVENT_TEXT_LIMIT)
        parts = []
        if command:
            parts.append(f"command={command}")
        if exit_code is not None:
            parts.append(f"exit_code={exit_code}")
        header = "; ".join(parts) or type(observation).__name__
        return f"Tool result: {header}\n{text}".rstrip()


def json_dumps_for_event(payload: dict[str, Any]) -> str:
    """JSON formatter for event payloads that may contain SDK objects."""
    import json

    return json.dumps(payload, sort_keys=True, default=str)
