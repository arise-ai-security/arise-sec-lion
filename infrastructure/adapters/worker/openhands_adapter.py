"""OpenHands SDK Adapter: use OpenHands agent via Python SDK for task execution.

Includes cost tracking via conversation.conversation_stats API.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from core.domain.events.events import DomainEvent

from .base import WorkerAdapterBase
from .shared import EventSequencer, get_model_pricing


# Suppress verbose OpenHands logging
logging.getLogger("openhands").setLevel(logging.WARNING)
logging.getLogger("openhands.sdk").setLevel(logging.WARNING)
logging.getLogger("openhands.tools").setLevel(logging.WARNING)


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
    """Execute tasks via OpenHands SDK with cost tracking.

    Cost tracking uses conversation.conversation_stats API which provides:
    - accumulated_cost: Total cost in USD
    - prompt_tokens: Input token count
    - completion_tokens: Output token count
    """

    STREAM_NAME = "openhands"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        """Initialize OpenHands adapter.

        Args:
            model: LLM model to use (default: gpt-4o via env or fallback).
            api_key: API key for LLM provider.
            timeout_seconds: Execution timeout.
        """
        super().__init__(timeout_seconds=timeout_seconds)
        self.model = model or os.getenv("LLM_MODEL", "openai/gpt-4o")
        self.api_key = api_key or self._detect_api_key()

    def _detect_api_key(self) -> str | None:
        """Detect appropriate API key based on model name.

        Returns:
            API key from environment, prioritizing provider-specific keys.
        """
        # Check for explicit LLM_API_KEY first
        if api_key := os.getenv("LLM_API_KEY"):
            return api_key

        model_lower = self.model.lower()

        # Anthropic/Claude models
        if "claude" in model_lower or "anthropic" in model_lower:
            return os.getenv("ANTHROPIC_API_KEY")

        # Google models
        if "gemini" in model_lower or "google" in model_lower:
            return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

        # Default to OpenAI
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
    ) -> AsyncIterator[DomainEvent]:
        """Execute task via OpenHands SDK with cost tracking."""
        self._start_timing()

        try:
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
            conversation = Conversation(agent=agent, workspace=working_dir)
            conversation.send_message(task_description)

            # Run with timeout
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, conversation.run),
                timeout=self.timeout_seconds,
            )

            # Yield thought events from conversation history
            if hasattr(conversation, "events"):
                for event in conversation.events:  # type: ignore[attr-defined]
                    content = self._extract_event_content(event)
                    if content and content.strip():
                        output_type = self._classify_event(event)
                        yield sequencer.thought(content.strip(), output_type)

            # Extract cost from conversation_stats
            cost_usd, tokens = self._extract_cost_data(conversation)

            # Emit cost event
            yield sequencer.cost_recorded(
                tool_name=self._get_tool_name(),
                cost_usd=cost_usd,
                duration_seconds=self._get_duration(),
                model=self.model,
                tokens=tokens,
            )

            # Emit completion
            result = self._extract_result(conversation, working_dir)
            yield sequencer.completed(result)

        except ImportError as e:
            yield sequencer.failed(
                f"OpenHands packages not installed. "
                f"Install with: uv add openhands-sdk openhands-tools. Error: {e}"
            )

        except TimeoutError:
            yield sequencer.failed(
                f"Task timed out after {self.timeout_seconds} seconds"
            )

        except Exception as e:
            yield sequencer.failed(f"OpenHands adapter error: {e!r}")

    def _extract_cost_data(self, conversation: Any) -> tuple[float, int | None]:
        """Extract cost from conversation_stats API.

        OpenHands SDK provides:
        - conversation.conversation_stats["accumulated_cost"]
        - conversation.conversation_stats["prompt_tokens"]
        - conversation.conversation_stats["completion_tokens"]

        Falls back to token-based calculation if accumulated_cost not available.
        """
        stats = getattr(conversation, "conversation_stats", None) or {}

        # Primary: use accumulated_cost from SDK (most accurate)
        accumulated_cost = stats.get("accumulated_cost")
        if accumulated_cost is not None:
            prompt_tokens = stats.get("prompt_tokens", 0)
            completion_tokens = stats.get("completion_tokens", 0)
            total_tokens = prompt_tokens + completion_tokens if (prompt_tokens or completion_tokens) else None
            return float(accumulated_cost), total_tokens

        # Fallback: calculate from tokens using our pricing registry
        prompt_tokens = stats.get("prompt_tokens", 0)
        completion_tokens = stats.get("completion_tokens", 0)

        if prompt_tokens or completion_tokens:
            pricing = get_model_pricing(self.model)
            cost = pricing.calculate_cost(prompt_tokens, completion_tokens)
            return cost, prompt_tokens + completion_tokens

        # No cost data available
        return 0.0, None

    def _extract_result(self, conversation: Any, working_dir: str) -> str:
        """Extract result from conversation events."""
        if hasattr(conversation, "events") and conversation.events:
            output_events = [
                self._extract_event_content(e)
                for e in conversation.events
                if self._classify_event(e) == "output"
            ]
            output_events = [e for e in output_events if e]
            if output_events:
                return "\n".join(output_events[-5:])
        return f"Task completed successfully in workspace: {working_dir}"

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
