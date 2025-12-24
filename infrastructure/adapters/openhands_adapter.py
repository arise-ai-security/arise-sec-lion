"""OpenHands SDK Adapter: use OpenHands agent via Python SDK for task execution."""

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
from infrastructure.adapters.worker_base import validate_task_context


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


class OpenHandsAdapter(WorkerToolPort):
    """Execute tasks via OpenHands SDK with configurable LLM provider."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.model = model or os.getenv("LLM_MODEL", "openai/gpt-4o")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _classify_event(event: Any) -> str:
        event_class_name = type(event).__name__
        return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")

    @staticmethod
    def _extract_event_content(event: Any) -> str | None:
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
        """Run task via OpenHands SDK, return (events, result, error)."""
        classified_events: list[tuple[str, str]] = []
        result: str | None = None
        error: str | None = None

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

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, conversation.run)

            if hasattr(conversation, "events"):
                for event in conversation.events:
                    content = self._extract_event_content(event)
                    if content:
                        output_type = self._classify_event(event)
                        classified_events.append((content, output_type))

            result = f"Task completed successfully in workspace: {working_dir}"
            if classified_events:
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
        """Execute task via OpenHands SDK, stream ThoughtCaptured, yield final event."""
        task_description, agent_id, working_dir = validate_task_context(task_context)

        try:
            classified_events, result, error = await asyncio.wait_for(
                self._run_with_sdk(task_description, working_dir),
                timeout=self.timeout_seconds,
            )

            sequence = 2
            for content, output_type in classified_events:
                if content.strip():
                    yield ThoughtCaptured(
                        aggregate_id=agent_id,
                        sequence_number=sequence,
                        content=content.strip(),
                        stream="openhands",
                        output_type=output_type,
                    )
                    sequence += 1

            if error:
                yield WorkFailed(
                    aggregate_id=agent_id,
                    sequence_number=sequence,
                    reason=error,
                )
            else:
                yield WorkCompleted(
                    aggregate_id=agent_id,
                    sequence_number=sequence,
                    result=result or "Task completed",
                )

        except TimeoutError:
            yield WorkFailed(
                aggregate_id=agent_id,
                sequence_number=2,
                reason=f"Task timed out after {self.timeout_seconds} seconds",
            )

        except Exception as e:
            yield WorkFailed(
                aggregate_id=agent_id,
                sequence_number=2,
                reason=f"OpenHands adapter error: {e!r}",
            )
