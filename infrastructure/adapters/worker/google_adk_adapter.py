"""Google ADK adapter using Gemini models plus filesystem and shell tools."""

import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from core.domain.events.events import DomainEvent

from .base import WorkerAdapterBase
from .shared import (
    ContainerSessionContext,
    EventSequencer,
    format_tool_event,
    get_model_pricing,
)


logger = logging.getLogger(__name__)


@dataclass
class ADKAdapterConfig:
    """Configuration for Google ADK adapter."""

    model: str = "gemini-3-pro"
    timeout_seconds: int = 300
    max_turns: int = 50
    allowed_tools: list[str] = field(
        default_factory=lambda: [
            "read_file",
            "write_file",
            "list_directory",
            "create_directory",
            "execute_command",
        ]
    )


def execute_command(
    command: str,
    working_dir: str | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """Execute shell command for building, compiling, or running code.

    This is a custom function tool for ADK that enables shell access
    for implementation, debugging, and verification tasks.

    Args:
        command: Shell command to execute (e.g., 'gcc -o poc poc.c', 'make', './poc')
        working_dir: Working directory for command execution
        timeout: Timeout in seconds (default 300 for long builds)

    Returns:
        Dict with stdout, stderr, exit_code, and status
    """
    try:
        result = subprocess.run(
            command,
            check=False, shell=True,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "status": "success" if result.returncode == 0 else "error",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "exit_code": -1,
        }
    except Exception as e:
        return {
            "status": "error",
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
        }


class GoogleADKAdapter(WorkerAdapterBase):
    """ADK-based adapter: uses Gemini models with MCP filesystem and shell tools.

    Maps ADK event types to ThoughtCaptured output_type values:
    - Text content -> "output"
    - Function calls -> "tool_use"
    - Function responses -> "tool_result"

    Supports general task execution with:
    - MCP filesystem server for file read/write/list operations
    - Custom execute_command tool for shell execution
    """

    STREAM_NAME = "google_adk"

    def __init__(self, config: ADKAdapterConfig | None = None) -> None:
        """Initialize ADK adapter.

        Args:
            config: Optional configuration. Uses defaults if not provided.
        """
        self.config = config or ADKAdapterConfig()
        super().__init__(timeout_seconds=self.config.timeout_seconds)

    def _get_tool_name(self) -> str:
        """Return tool identifier for cost tracking."""
        return "google_adk"

    async def _execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        """Execute task via ADK, stream ThoughtCaptured events, yield final event.

        Uses MCP filesystem tools and custom shell execution.
        """
        self._start_timing()
        container_session = ContainerSessionContext.from_task_context(task_context)
        if container_session is not None:
            task_description = container_session.apply_task_prefix(
                task_description,
                auto_shell=True,
            )

        try:
            # Import ADK modules (lazy import to avoid import errors if not installed)
            from google.adk.agents import LlmAgent
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.adk.tools.mcp_tool import McpToolset
            from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
            from google.genai import types as genai_types
            from mcp import StdioServerParameters

            # Create shell tool bound to working directory
            shell_tool = self._create_shell_tool(working_dir, container_session)

            # Create agent with MCP filesystem + shell tools
            agent = LlmAgent(
                model=self.config.model,
                name="worker_agent",
                instruction=self._build_instruction(),
                tools=[
                    # MCP Filesystem tools
                    McpToolset(
                        connection_params=StdioConnectionParams(
                            server_params=StdioServerParameters(
                                command="npx",
                                args=[
                                    "-y",
                                    "@modelcontextprotocol/server-filesystem",
                                    working_dir,
                                ],
                            ),
                            timeout=30,
                        ),
                    ),
                    # Custom shell execution tool
                    shell_tool,
                ],
            )

            # Create session service and runner
            session_service = InMemorySessionService()
            runner = Runner(
                agent=agent,
                app_name="arise_worker",
                session_service=session_service,
            )
            session_id = str(uuid4())
            user_id = "worker"

            # Track token usage for cost calculation
            total_input_tokens = 0
            total_output_tokens = 0
            result_text = ""

            # Create the session first
            await session_service.create_session(
                app_name="arise_worker",
                user_id=user_id,
                session_id=session_id,
            )

            # Create Content from task description
            message_content = genai_types.Content(
                parts=[genai_types.Part(text=task_description)],
                role="user",
            )

            # Run the agent with timeout
            async def run_agent() -> None:
                nonlocal total_input_tokens, total_output_tokens, result_text

                async for event in runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=message_content,
                ):
                    # Extract token usage if available
                    usage = getattr(event, "usage_metadata", None)
                    if usage:
                        input_count = getattr(usage, "prompt_token_count", None)
                        output_count = getattr(usage, "candidates_token_count", None)
                        if input_count is not None:
                            total_input_tokens += input_count
                        if output_count is not None:
                            total_output_tokens += output_count

                    # Extract content for result
                    content = getattr(event, "content", None)
                    if content:
                        if isinstance(content, str):
                            result_text = content
                        elif hasattr(content, "text"):
                            result_text = content.text
                        elif hasattr(content, "parts"):
                            for part in content.parts:
                                if hasattr(part, "text") and part.text:
                                    result_text = part.text

                    # Yield domain events
                    for domain_event in self._process_adk_event(event, sequencer):
                        # Events need to be yielded in the outer generator
                        pass

            try:
                await asyncio.wait_for(
                    run_agent(),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError:
                yield sequencer.failed(
                    f"Task timed out after {self.config.timeout_seconds} seconds"
                )
                return

            # Emit a summary thought event
            if result_text:
                yield sequencer.thought(result_text, "output")

            # Emit cost event if we have token data
            total_tokens = total_input_tokens + total_output_tokens
            if total_tokens > 0:
                pricing = get_model_pricing(self.config.model)
                cost_usd = pricing.calculate_cost(total_input_tokens, total_output_tokens)
                yield sequencer.cost_recorded(
                    tool_name=self._get_tool_name(),
                    cost_usd=cost_usd,
                    duration_seconds=self._get_duration(),
                    model=self.config.model,
                    tokens=total_tokens,
                )

            # Emit terminal event
            yield sequencer.completed(result_text or "Task completed")

        except ImportError as e:
            yield sequencer.failed(
                f"Google ADK packages not installed. "
                f"Install with: uv add google-adk mcp. Error: {e}"
            )

        except Exception as e:
            logger.error(f"ADK execution error: {e}")
            yield sequencer.failed(f"Google ADK adapter error: {e!r}")

    def _build_instruction(self) -> str:
        """Build the agent instruction for general task execution."""
        return """You are a task execution agent.

You have access to:
1. Filesystem tools (read_file, write_file, list_directory, create_directory)
2. Shell command execution (shell_execute) for building, compiling, and running code

Use those tools to inspect the workspace, make changes, run builds/tests, and
verify the task outcome.

Always explain your reasoning and provide clear output about what you're doing."""

    def _create_shell_tool(
        self,
        working_dir: str,
        container_session: ContainerSessionContext | None = None,
    ) -> Any:
        """Create shell execution tool with bound working directory."""

        def shell_execute(command: str, timeout: int = 300) -> dict[str, Any]:
            """Execute shell command in the workspace.

            Args:
                command: Shell command to execute
                timeout: Timeout in seconds (default 300)

            Returns:
                Dict with stdout, stderr, exit_code, and status
            """
            if container_session is not None:
                command = container_session.wrap_shell_command(command)
            return execute_command(command, working_dir=working_dir, timeout=timeout)

        return shell_execute

    def _process_adk_event(
        self, event: Any, sequencer: EventSequencer
    ) -> list[DomainEvent]:
        """Process ADK event and return domain events."""
        events: list[DomainEvent] = []

        # Handle content events
        content = getattr(event, "content", None)
        if content is None:
            return events

        # String content
        if isinstance(content, str):
            if content.strip():
                events.append(sequencer.thought(content, "output"))
            return events

        # Content with text attribute
        if hasattr(content, "text"):
            text = getattr(content, "text", "")
            if text and text.strip():
                events.append(sequencer.thought(text, "output"))
            return events

        # Content with parts (multimodal)
        if hasattr(content, "parts"):
            for part in content.parts:
                # Text part
                if hasattr(part, "text"):
                    text = getattr(part, "text", "")
                    if text and text.strip():
                        events.append(sequencer.thought(text, "output"))

                # Function call part
                elif hasattr(part, "function_call"):
                    fc = part.function_call
                    tool_name = getattr(fc, "name", "unknown")
                    tool_args = getattr(fc, "args", {})
                    if isinstance(tool_args, dict):
                        tool_input = tool_args
                    else:
                        tool_input = dict(tool_args) if tool_args else {}
                    content_str = format_tool_event(tool_name, tool_input)
                    events.append(sequencer.thought(content_str, "tool_use"))

                # Function response part
                elif hasattr(part, "function_response"):
                    fr = part.function_response
                    response = getattr(fr, "response", "")
                    response_text = str(response)[:500] if response else ""
                    events.append(
                        sequencer.thought(f"Tool result: {response_text}", "tool_result")
                    )

        return events
