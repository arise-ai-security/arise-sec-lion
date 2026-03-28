"""OpenHands SDK adapter: use the SDK conversation directly for task execution."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from core.domain.events.events import DomainEvent

from .base import WorkerAdapterBase
from .shared import ContainerSessionContext, EventSequencer, get_model_pricing


# Suppress verbose OpenHands logging
logging.getLogger("openhands").setLevel(logging.WARNING)
logging.getLogger("openhands.sdk").setLevel(logging.WARNING)
logging.getLogger("openhands.tools").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS_PER_RUN = 20


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
        self.api_key = api_key or self._detect_api_key()
        self.max_iterations_per_run = max_iterations_per_run

    def _detect_api_key(self) -> str | None:
        """Detect appropriate API key based on model name."""
        if api_key := os.getenv("LLM_API_KEY"):
            return api_key

        model_lower = self.model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return os.getenv("ANTHROPIC_API_KEY")
        if "gemini" in model_lower or "google" in model_lower:
            return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
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
            conversation = self._build_conversation(working_dir)
        except ImportError as error:
            yield sequencer.failed(
                "OpenHands packages not installed. "
                f"Install with: uv add openhands-sdk openhands-tools. Error: {error}"
            )
            return
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {error!r}")
            return

        try:
            conversation.send_message(task_description)

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(conversation.run),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError:
                self._request_shutdown(conversation)
                yield sequencer.failed(
                    f"Task timed out after {self.timeout_seconds} seconds"
                )
                return

            for event in self._iter_conversation_events(conversation):
                content = self._extract_event_content(event)
                if content and content.strip():
                    yield sequencer.thought(
                        content.strip(),
                        self._classify_event(event),
                    )

            cost_usd, tokens = self._extract_cost_data(conversation)
            yield sequencer.cost_recorded(
                tool_name=self._get_tool_name(),
                cost_usd=cost_usd,
                duration_seconds=self._get_duration(),
                model=self.model,
                tokens=tokens,
            )

            yield sequencer.completed(self._extract_result(conversation, working_dir))
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {error!r}")
        finally:
            self._request_shutdown(conversation)

    def _build_conversation(self, working_dir: str) -> Any:
        """Create an OpenHands SDK conversation for the current task."""
        from openhands.sdk import LLM, Agent, Conversation, Tool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.terminal import TerminalTool

        llm = LLM(model=self.model, api_key=self.api_key)
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

    def _extract_cost_data(self, conversation: Any) -> tuple[float, int | None]:
        """Extract cost from conversation_stats API."""
        stats = getattr(conversation, "conversation_stats", None)
        if stats is None:
            return 0.0, None

        def get_stat(name: str, default: Any = None) -> Any:
            if isinstance(stats, dict):
                return stats.get(name, default)
            return getattr(stats, name, default)

        accumulated_cost = get_stat("accumulated_cost")
        if accumulated_cost is not None:
            prompt_tokens = get_stat("prompt_tokens", 0) or 0
            completion_tokens = get_stat("completion_tokens", 0) or 0
            total_tokens = (
                prompt_tokens + completion_tokens
                if (prompt_tokens or completion_tokens)
                else None
            )
            return float(accumulated_cost), total_tokens

        prompt_tokens = get_stat("prompt_tokens", 0) or 0
        completion_tokens = get_stat("completion_tokens", 0) or 0
        if prompt_tokens or completion_tokens:
            pricing = get_model_pricing(self.model)
            cost = pricing.calculate_cost(prompt_tokens, completion_tokens)
            return cost, prompt_tokens + completion_tokens

        return 0.0, None

    def _extract_result(self, conversation: Any, working_dir: str) -> str:
        """Extract result from conversation events."""
        output_events = [
            self._extract_event_content(event)
            for event in self._iter_conversation_events(conversation)
            if self._classify_event(event) == "output"
        ]
        output_events = [event for event in output_events if event]
        if output_events:
            return "\n".join(output_events[-5:])
        return f"Task completed successfully in workspace: {working_dir}"

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

    @staticmethod
    def _classify_event(event: Any) -> str:
        """Classify SDK event type to output_type."""
        event_class_name = type(event).__name__
        return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")

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
