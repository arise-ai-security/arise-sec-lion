"""LiteLLM-based research adapter with tool calling.

This adapter implements the ResearchPort for RESEARCHER agents, providing
read-only tool calling capabilities for context gathering. Tool execution
is delegated to OpenHands executors via ResearchToolRegistry.
"""

import json
import logging
from pathlib import Path
from typing import Any

import litellm
from jinja2 import Environment, FileSystemLoader

from core.domain.exceptions import LLMError
from core.ports.research_port import ResearchPort, ResearchResult
from infrastructure.adapters.research.tool_registry import ResearchToolRegistry

logger = logging.getLogger(__name__)


class LiteLLMResearchAdapter(ResearchPort):
    """Research adapter using LiteLLM tool calling.

    Responsibilities (Single Responsibility):
    - Orchestrate LLM tool calling loop
    - Delegate tool execution to ResearchToolRegistry
    - Track token usage

    Tool definitions and execution are handled by ResearchToolRegistry.
    Prompt template is loaded from prompts/core/roles/researcher.j2.
    """

    def __init__(
        self,
        default_config: dict[str, Any] | None = None,
        prompts_dir: str = "prompts",
    ) -> None:
        """Initialize research adapter.

        Args:
            default_config: Default LLM configuration
            prompts_dir: Directory containing prompt templates
        """
        self._default_config = default_config or {}
        self._system_prompt = self._load_system_prompt(prompts_dir)

    def _load_system_prompt(self, prompts_dir: str) -> str:
        """Load system prompt from template file."""
        template_path = Path(prompts_dir)
        if not template_path.exists():
            logger.warning("Prompts directory not found: %s", prompts_dir)
            return self._default_system_prompt()

        env = Environment(loader=FileSystemLoader(str(template_path)))
        try:
            template = env.get_template("core/roles/researcher.j2")
            return template.render()
        except Exception as e:
            logger.warning("Failed to load researcher prompt: %s", e)
            return self._default_system_prompt()

    def _default_system_prompt(self) -> str:
        """Fallback system prompt if template loading fails."""
        return (
            "You are a research assistant gathering context for a software engineering task. "
            "Analyze the codebase and gather relevant information to inform task planning."
        )

    async def run_research_session(
        self,
        task_description: str,
        working_directory: str,
        config_dict: dict[str, Any],
        available_tools: list[str] | None = None,
        max_tool_calls: int = 10,
    ) -> ResearchResult:
        """Execute research with tool calling loop.

        Args:
            task_description: The task to research
            working_directory: Base directory for file operations
            config_dict: LLM configuration
            available_tools: Tools to enable (default: all)
            max_tool_calls: Maximum tool calls before forcing summary

        Returns:
            ResearchResult with findings and metadata
        """
        merged_config = {**self._default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")

        # Initialize tool registry for this session
        tool_registry = ResearchToolRegistry(working_directory)
        tools = tool_registry.get_tools(available_tools)

        # Build messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": f"Task to research:\n\n{task_description}"},
        ]

        tool_calls_made: list[dict[str, Any]] = []
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for iteration in range(max_tool_calls):
            response = await self._call_llm(model, messages, tools, merged_config)

            # Track tokens
            usage = response.usage
            total_prompt_tokens += usage.prompt_tokens if usage else 0
            total_completion_tokens += usage.completion_tokens if usage else 0

            # Handle empty choices
            if not response.choices:
                raise LLMError("LLM returned empty choices list - no response generated")

            message = response.choices[0].message

            # If no tool calls, research is complete
            if not message.tool_calls:
                logger.info(
                    "Research completed after %d tool calls, %d iterations",
                    len(tool_calls_made),
                    iteration + 1,
                )
                return ResearchResult(
                    findings=message.content or "No findings",
                    tool_calls=tool_calls_made,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                )

            # Process tool calls
            messages.append(message.model_dump())
            tool_calls_made.extend(
                self._execute_tool_calls(message.tool_calls, tool_registry, messages)
            )

        # Max iterations reached - request final summary
        return await self._request_summary(
            model, messages, merged_config, tool_calls_made,
            total_prompt_tokens, total_completion_tokens
        )

    async def _call_llm(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
    ) -> Any:
        """Make LLM API call with error handling."""
        try:
            return await litellm.acompletion(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                temperature=config.get("temperature", 0.0),
                max_tokens=config.get("max_tokens", 4000),
            )
        except litellm.exceptions.AuthenticationError as e:
            raise LLMError(f"Research LLM authentication failed: {e}", original_error=e) from e
        except litellm.exceptions.RateLimitError as e:
            raise LLMError(f"Research LLM rate limit exceeded: {e}", original_error=e) from e
        except litellm.exceptions.APIError as e:
            raise LLMError(f"Research LLM API error: {e}", original_error=e) from e
        except Exception as e:
            raise LLMError(f"Research LLM error: {e}", original_error=e) from e

    def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        tool_registry: ResearchToolRegistry,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Execute tool calls and append results to messages.

        Returns list of tool call records for tracking.
        """
        records = []

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Failed to parse tool arguments for %s: %s (raw: %s)",
                    function_name, e, tool_call.function.arguments[:200],
                )
                arguments = {}

            logger.debug("Executing tool: %s(%s)", function_name, arguments)

            # Execute via registry
            result = tool_registry.execute(function_name, arguments)

            # Track tool call
            records.append({
                "tool": function_name,
                "arguments": arguments,
                "result": result[:2000] if len(result) > 2000 else result,
            })

            # Append tool result (OpenAI format)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        return records

    async def _request_summary(
        self,
        model: str,
        messages: list[dict[str, Any]],
        config: dict[str, Any],
        tool_calls_made: list[dict[str, Any]],
        total_prompt_tokens: int,
        total_completion_tokens: int,
    ) -> ResearchResult:
        """Request final summary when max iterations reached."""
        logger.info("Max tool calls reached, requesting summary")

        messages.append({
            "role": "user",
            "content": "You've reached the maximum number of tool calls. "
                       "Please summarize your research findings based on the information gathered so far.",
        })

        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                temperature=0.0,
                max_tokens=config.get("max_tokens", 4000),
            )
        except Exception as e:
            raise LLMError(f"Research summary failed: {e}", original_error=e) from e

        usage = response.usage
        total_prompt_tokens += usage.prompt_tokens if usage else 0
        total_completion_tokens += usage.completion_tokens if usage else 0

        return ResearchResult(
            findings=response.choices[0].message.content or "No findings",
            tool_calls=tool_calls_made,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
        )
