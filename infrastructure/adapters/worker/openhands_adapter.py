"""OpenHands SDK adapter: use the SDK conversation directly for task execution."""

import asyncio
import concurrent.futures
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
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

# Cap for per-thought content captured from SDK events. Mirrors the Claude SDK
# adapter (10 KiB) so downstream dataset analysis applies one truncation rule
# across both arms of the tree-vs-flat experiment.
_THOUGHT_CONTENT_CAP = 10_240
_TRUNCATION_MARKER = "…[TRUNCATED]"


SDK_EVENT_TYPE_MAP: dict[str, str] = {
    "ActionEvent": "thinking",
    "AgentThinkAction": "thinking",
    "MessageEvent": "output",
    "ObservationEvent": "output",
    "CmdRunObservation": "output",
    "AgentErrorEvent": "output",
    "FileReadAction": "progress",
    "FileWriteAction": "progress",
    "FileEditAction": "progress",
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
    ) -> None:
        """Initialize OpenHands adapter."""
        super().__init__(timeout_seconds=timeout_seconds)
        self.model = model or os.getenv("LLM_MODEL", "openai/gpt-4o")
        self._explicit_api_key = api_key
        self.max_iterations_per_run = max_iterations_per_run

    def _detect_api_key(self, model: str) -> str | None:
        """Detect appropriate API key based on model name."""
        if api_key := os.getenv("LLM_API_KEY"):
            return api_key

        model_lower = model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return os.getenv("ANTHROPIC_API_KEY")
        if "gemini" in model_lower or "google" in model_lower:
            return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        return os.getenv("OPENAI_API_KEY")

    def _resolve_api_key(self, model: str) -> str | None:
        """Resolve the API key for the selected runtime model."""
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        return self._detect_api_key(model)

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
        runtime_model = self._resolve_runtime_model(task_context, self.model) or self.model
        container_session = ContainerSessionContext.from_task_context(task_context)
        if container_session is not None:
            task_description = container_session.apply_task_prefix(
                task_description,
                auto_shell=False,
            )

        try:
            conversation = self._build_conversation(
                working_dir,
                model=runtime_model,
                api_key=self._resolve_api_key(runtime_model),
            )
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
            conversation.send_message(task_description)
            future = executor.submit(conversation.run)

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
                    stripped = content.strip()
                    capped, was_truncated, result_bytes = self._cap_thought_content(
                        stripped,
                    )
                    yield sequencer.thought(
                        capped,
                        self._classify_event(event),
                        was_truncated=was_truncated,
                        result_bytes=result_bytes,
                    )
                # Capture finish message during iteration
                if finish_message is None:
                    finish_message = self._try_extract_finish(event)

            cost_data = self._extract_cost_data(conversation)
            yield sequencer.cost_recorded(
                tool_name=self._get_tool_name(),
                cost_usd=cost_data.cost_usd,
                duration_seconds=self._get_duration(),
                model=self._resolve_cost_model(cost_data, runtime_model),
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

    def _build_conversation(
        self,
        working_dir: str,
        *,
        model: str,
        api_key: str | None,
    ) -> Any:
        """Create an OpenHands SDK conversation for the current task."""
        from openhands.sdk import LLM, Agent, Conversation, Tool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.terminal import TerminalTool

        llm = LLM(model=model, api_key=api_key)
        agent = Agent(
            llm=llm,
            tools=[
                Tool(name=TerminalTool.name),
                Tool(name=FileEditorTool.name),
            ],
        )
        return Conversation(
            agent=agent,
            workspace=working_dir,
            max_iteration_per_run=self.max_iterations_per_run,
        )

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

    def _resolve_cost_model(
        self,
        cost_data: OpenHandsCostData,
        runtime_model: str | None,
    ) -> str | None:
        """Return a representative model value for the aggregate worker event."""
        usage_models = {usage.model for usage in cost_data.usage_metrics if usage.model}
        if len(usage_models) == 1:
            return next(iter(usage_models))
        return runtime_model

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
            if self._classify_event(event) != "output":
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
        event_class_name = type(event).__name__
        return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")

    @staticmethod
    def _cap_thought_content(content: str) -> tuple[str, bool, int]:
        """Cap per-thought content, returning ``(capped, was_truncated, result_bytes)``.

        ``result_bytes`` is the pre-truncation length so downstream loss
        accounting stays honest. ``was_truncated`` reports whether the cap
        actually fired. Schema-parity with the Claude SDK adapter (same cap,
        same truncation marker) keeps Pillar B analysis code adapter-agnostic.
        """
        result_bytes = len(content)
        if result_bytes <= _THOUGHT_CONTENT_CAP:
            return content, False, result_bytes
        return (
            content[:_THOUGHT_CONTENT_CAP] + _TRUNCATION_MARKER,
            True,
            result_bytes,
        )

    @staticmethod
    def _extract_event_content(event: Any) -> str | None:
        """Extract content from SDK event."""
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
