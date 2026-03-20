"""Agentic tool-calling loop for manager reconnaissance.

Manages the LLM ↔ tool loop: sends prompt with tool definitions to the LLM,
dispatches tool calls to ReconToolPort, appends results, and repeats until
the LLM produces a final text response (the assessment/decomposition JSON).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from core.domain.values.llm_response import LLMResponse, LLMToolResponse, LLMUsage
from core.ports.runtime_ports import LLMPort, ReconToolPort

logger = logging.getLogger(__name__)

_ZERO_USAGE = LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)


@dataclass(frozen=True)
class ReconToolRecord:
    """Record of a single tool call for event emission."""

    tool_name: str
    arguments: dict[str, Any]
    result_summary: str


@dataclass(frozen=True)
class ToolCallingResult:
    """Result of the tool-calling loop: final response + tool call history."""

    response: LLMResponse
    tool_records: list[ReconToolRecord] = field(default_factory=list)


class ToolCallingService:
    """Coordinates multi-turn LLM tool calling for reconnaissance.

    The service owns the agentic loop:
    1. Send system+user messages with tool definitions to LLM
    2. If LLM returns tool_calls → execute via ReconToolPort → append results
    3. Loop until LLM returns a text response (final answer)
    4. Return final content + aggregated usage/cost + tool call records
    """

    def __init__(
        self,
        llm_port: LLMPort,
        recon_port: ReconToolPort,
        max_iterations: int = 10,
    ) -> None:
        self._llm_port = llm_port
        self._recon_port = recon_port
        self._max_iterations = max_iterations

    async def run_with_tools(
        self,
        prompt: str,
        config_dict: dict[str, Any],
    ) -> ToolCallingResult:
        """Run the agentic tool-calling loop.

        Args:
            prompt: The full prompt text (system + context assembled by PromptBuilder).
            config_dict: Model config (model, temperature, max_tokens).

        Returns:
            ToolCallingResult with final LLMResponse and tool call records for
            event emission.
        """
        tool_defs = self._recon_port.get_tool_definitions()
        tool_records: list[ReconToolRecord] = []

        # Start with the prompt as a user message
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]

        total_usage = _ZERO_USAGE
        total_cost = 0.0
        model = config_dict.get("model", "unknown")

        for iteration in range(self._max_iterations):
            response: LLMToolResponse = await self._llm_port.query_with_tools(
                messages=messages,
                config_dict=config_dict,
                tools=tool_defs,
            )

            total_usage = total_usage + response.usage
            total_cost += response.cost_usd
            model = response.model

            # If no tool calls, we have the final answer
            if not response.has_tool_calls:
                content = response.content or ""
                return ToolCallingResult(
                    response=LLMResponse(
                        content=content,
                        usage=total_usage,
                        model=model,
                        cost_usd=round(total_cost, 6),
                    ),
                    tool_records=tool_records,
                )

            # Append assistant message with tool calls (for conversation history)
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": _serialize_arguments(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            if response.content:
                assistant_msg["content"] = response.content
            messages.append(assistant_msg)

            # Execute each tool call and append results
            for tc in response.tool_calls:
                logger.info(
                    "Recon tool call [iter=%d]: %s(%s)",
                    iteration,
                    tc.name,
                    _summarize_args(tc.arguments),
                )
                result = await self._recon_port.execute_tool(tc.name, tc.arguments)

                # Record for event emission
                summary = result[:500] if len(result) > 500 else result
                tool_records.append(ReconToolRecord(
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    result_summary=summary,
                ))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        # Hit max iterations — force a final answer without tools
        logger.warning(
            "Tool-calling loop hit max iterations (%d), forcing final answer",
            self._max_iterations,
        )
        messages.append({
            "role": "user",
            "content": (
                "You have reached the maximum number of tool calls. "
                "You MUST now provide your final answer as the JSON object "
                "described in the output_format section above. Do NOT return "
                "a status message — return the actual assessment/decomposition JSON. "
                "Use whatever information you have gathered so far."
            ),
        })

        # Final call without tools to force a text response
        fallback = await self._llm_port.query_with_tools(
            messages=messages,
            config_dict=config_dict,
            tools=[],  # No tools available → forces text response
        )
        total_usage = total_usage + fallback.usage
        total_cost += fallback.cost_usd

        return ToolCallingResult(
            response=LLMResponse(
                content=fallback.content or "",
                usage=total_usage,
                model=model,
                cost_usd=round(total_cost, 6),
            ),
            tool_records=tool_records,
        )


def _serialize_arguments(arguments: dict[str, Any]) -> str:
    """Serialize tool call arguments to JSON string for message history."""
    return json.dumps(arguments)


def _summarize_args(arguments: dict[str, Any]) -> str:
    """Create a short summary of arguments for logging."""
    parts = []
    for k, v in arguments.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)
