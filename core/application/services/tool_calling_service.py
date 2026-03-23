"""Agentic tool-calling loop for manager reconnaissance.

Manages the LLM ↔ tool loop: sends prompt with tool definitions to the LLM,
dispatches tool calls to ReconToolPort, appends results, and repeats until
the LLM produces a final text response (the assessment/decomposition JSON).

Context management (inspired by Claude Code / OpenHands):
  Layer 1 — Per-result truncation: hard-cap each tool output
  Layer 2 — Sliding-window condensation: LLM-summarize older tool exchanges
  Layer 3 — Token budget gate: force-condense when near context limit
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.domain.values.llm_response import LLMResponse, LLMToolResponse, LLMUsage
from core.domain.values.recon_policy import ReconPolicy
from core.ports.runtime_ports import LLMPort, ReconToolPort

if TYPE_CHECKING:
    from core.application.services.context_condenser import ContextCondenser

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

    Context management layers (optional, via ``condenser``):
    - Each tool result is truncated before entering the history
    - After N iterations, older exchanges are LLM-summarized
    - If estimated tokens exceed budget, force-condense before the next call
    """

    def __init__(
        self,
        llm_port: LLMPort,
        recon_port: ReconToolPort,
        max_iterations: int = 10,
        condenser: ContextCondenser | None = None,
    ) -> None:
        self._llm_port = llm_port
        self._recon_port = recon_port
        self._max_iterations = max_iterations
        self._condenser = condenser

    async def run_with_tools(
        self,
        prompt: str,
        config_dict: dict[str, Any],
        policy: ReconPolicy | None = None,
    ) -> ToolCallingResult:
        """Run the agentic tool-calling loop.

        Args:
            prompt: The full prompt text (system + context assembled by PromptBuilder).
            config_dict: Model config (model, temperature, max_tokens).
            policy: Per-call recon policy. When provided, overrides the instance
                defaults for max_iterations, result_char_limit, and allowed_tools.

        Returns:
            ToolCallingResult with final LLMResponse and tool call records for
            event emission.
        """
        # Policy overrides instance defaults
        max_iterations = policy.max_iterations if policy else self._max_iterations
        result_char_limit = policy.result_char_limit if policy else None

        tool_defs = self._recon_port.get_tool_definitions()

        # Filter tool definitions by policy.allowed_tools
        if policy and policy.allowed_tools is not None:
            tool_defs = [
                td for td in tool_defs
                if td.get("function", {}).get("name") in policy.allowed_tools
            ]

        tool_records: list[ReconToolRecord] = []

        # Start with the prompt as a user message
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]

        total_usage = _ZERO_USAGE
        total_cost = 0.0
        model = config_dict.get("model", "unknown")

        for iteration in range(max_iterations):
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

            # Observation masking: before executing new tools, replace
            # previous iterations' tool results with 1-line stubs.
            # This is cheaper and more effective than LLM summarization
            # (JetBrains "Complexity Trap" finding).
            if iteration > 0:
                messages = _mask_old_tool_results(messages)

            # Execute each tool call and append results
            for tc in response.tool_calls:
                logger.info(
                    "Recon tool call [iter=%d]: %s(%s)",
                    iteration,
                    tc.name,
                    _summarize_args(tc.arguments),
                )
                result = await self._recon_port.execute_tool(tc.name, tc.arguments)

                # Layer 1: truncate result before it enters history
                if self._condenser is not None:
                    result = self._condenser.truncate_result(
                        result, char_limit=result_char_limit,
                    )

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

            # Layer 2: condense older tool exchanges after threshold
            if self._condenser is not None:
                messages = await self._condenser.maybe_condense(
                    messages, current_iteration=iteration,
                )

            # Layer 3: if still over budget after condensation, force stop
            if self._condenser is not None and self._condenser.exceeds_budget(messages):
                logger.warning(
                    "Token budget exceeded (~%d tokens), forcing final answer",
                    self._condenser.estimate_total_tokens(messages),
                )
                break

        # Hit max iterations (or budget exceeded) — force a final answer without tools
        logger.warning(
            "Tool-calling loop hit max iterations (%d), forcing final answer",
            max_iterations,
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


def _mask_old_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace old tool results with 1-line stubs, keep most recent verbatim.

    Walks backwards to find the last assistant+tool block boundary.
    Everything before that boundary gets its tool result content replaced
    with a short stub showing only the tool call name.

    This implements observation masking — the JetBrains "Complexity Trap"
    finding shows this halves token cost while matching or exceeding LLM
    summarization in effectiveness.
    """
    # Find the start of the most recent assistant+tool block
    last_assistant_idx = None
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].get("role") == "assistant" and "tool_calls" in messages[i]:
            last_assistant_idx = i
            break

    if last_assistant_idx is None or last_assistant_idx <= 1:
        return messages

    # Build masked copy
    result: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i < last_assistant_idx and msg.get("role") == "tool":
            # Mask old tool results
            tool_id = msg.get("tool_call_id", "?")
            # Find the tool name from the preceding assistant message
            tool_name = _find_tool_name(messages, tool_id)
            content = msg.get("content", "")
            line_count = content.count("\n") + 1
            result.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": f"[MASKED — {tool_name}: {line_count} lines, see earlier call]",
            })
        else:
            result.append(msg)

    return result


def _find_tool_name(messages: list[dict[str, Any]], tool_call_id: str) -> str:
    """Find tool name for a tool_call_id from assistant messages."""
    for msg in messages:
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                if tc.get("id") == tool_call_id:
                    return tc.get("function", {}).get("name", "unknown")
    return "unknown"


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
