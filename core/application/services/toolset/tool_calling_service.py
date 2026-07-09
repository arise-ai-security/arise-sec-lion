"""Agentic tool-calling loop for manager toolsets.

Manages the LLM ↔ tool loop: sends prompt with tool definitions to the LLM,
dispatches tool calls to the owning toolset, appends results, and repeats
until the LLM produces a final text response.

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

from core.application.services.toolset.toolset_context import ActiveToolContext
from core.domain.values.llm_response import LLMResponse, LLMToolResponse, LLMUsage
from core.ports.runtime_ports import LLMPort


if TYPE_CHECKING:
    from core.application.services.orchestration.context_condenser import ContextCondenser

logger = logging.getLogger(__name__)

_ZERO_USAGE = LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)


@dataclass(frozen=True)
class ToolRecord:
    """Record of a single tool call for event emission."""

    tool_name: str
    arguments: dict[str, Any]
    result_summary: str


@dataclass(frozen=True)
class CapturedRead:
    """A source-read tool call captured with its FULL (untruncated) result.

    ``tool_records`` keep only a 500-char summary; this carries the verbatim
    content so the caller can persist it into the shared code-prefix block
    (the manager read-once → propagate-to-children path). Populated only for
    tools named in ``ActiveToolContext.source_read_tools``.
    """

    tool_name: str
    arguments: dict[str, Any]
    content: str


@dataclass(frozen=True)
class ToolCallingResult:
    """Result of the tool-calling loop: final response + tool call history."""

    response: LLMResponse
    tool_records: list[ToolRecord] = field(default_factory=list)
    captured_reads: list[CapturedRead] = field(default_factory=list)


class ToolCallingService:
    """Coordinates multi-turn LLM tool calling for any active toolset.

    The service owns the agentic loop:
    1. Send system+user messages with tool definitions to LLM
    2. If LLM returns tool_calls → execute via owning toolset → append results
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
        condenser: ContextCondenser | None = None,
    ) -> None:
        self._llm_port = llm_port
        self._condenser = condenser

    async def run_with_tools(
        self,
        prompt: str,
        config_dict: dict[str, Any],
        tool_context: ActiveToolContext,
    ) -> ToolCallingResult:
        """Run the agentic tool-calling loop.

        Args:
            prompt: The full prompt text (system + context assembled by PromptBuilder).
            config_dict: Model config (model, temperature, max_tokens).
            tool_context: Resolved tool definitions, loop policy, and executor map
                for this query.

        Returns:
            ToolCallingResult with final LLMResponse and tool call records for
            event emission.
        """
        max_iterations = max(tool_context.loop_policy.max_iterations, 1)
        result_char_limit = tool_context.loop_policy.result_char_limit
        tool_defs = list(tool_context.tool_definitions)

        tool_records: list[ToolRecord] = []
        captured_reads: list[CapturedRead] = []
        source_read_tools = tool_context.source_read_tools
        # Append-only caching invariant: the message tail must stay byte-stable
        # across rounds so the Anthropic rolling cache breakpoint reads it back.
        # ``frozen_msg_indices`` tracks transient tool results already elided
        # (elide once, never re-touch); ``seen_read_paths`` dedups source reads
        # of the same path to a pointer instead of re-sending the content.
        frozen_msg_indices: set[int] = set()
        seen_read_paths: dict[str, int] = {}

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
                    captured_reads=captured_reads,
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

            # Elide aged transient tool results ONCE, in place — keeps older
            # messages byte-stable so the prompt-cache prefix survives. Source
            # reads (read_file) are never elided; they stay verbatim and are
            # deduped at append time below.
            _freeze_aged_tool_results(messages, source_read_tools, frozen_msg_indices)

            # Execute each tool call and append results
            for tc in response.tool_calls:
                logger.info(
                    "Tool call [iter=%d]: %s(%s)",
                    iteration,
                    tc.name,
                    _summarize_args(tc.arguments),
                )
                executor = tool_context.executors.get(tc.name)
                if executor is None:
                    result = json.dumps({"error": f"Unknown tool: {tc.name}"})
                else:
                    result = await executor.execute_tool(tc.name, tc.arguments)

                is_source = tc.name in source_read_tools
                if is_source:
                    # Capture the FULL result for the shared block (verbatim).
                    captured_reads.append(
                        CapturedRead(tool_name=tc.name, arguments=tc.arguments, content=result)
                    )
                    path = tc.arguments.get("path")
                    if isinstance(path, str) and path in seen_read_paths:
                        # Same path already read in this loop — point at the
                        # retained copy instead of re-sending it, so the model
                        # never pays to re-read source it already has in context.
                        content = (
                            f"[read_file('{path}') — identical to the copy already "
                            "provided above in this session; not re-sent]"
                        )
                    else:
                        # Keep source verbatim (no Layer-1 truncation): it is the
                        # content decomposition reasons over, and it is cheap once
                        # the rolling cache breakpoint serves it as cache_read.
                        content = result
                        if isinstance(path, str):
                            seen_read_paths[path] = len(messages)
                else:
                    # Transient navigation result — Layer-1 truncation applies.
                    content = result
                    if self._condenser is not None:
                        content = self._condenser.truncate_result(
                            content, char_limit=result_char_limit,
                        )

                summary = content[:500] if len(content) > 500 else content
                tool_records.append(ToolRecord(
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    result_summary=summary,
                ))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                })

            # Budget gate: force a final answer if the loop is over budget.
            # (Sliding-window LLM condensation was removed — it rebuilt the
            # message tail each call, churning the bytes the rolling cache
            # breakpoint relies on. Per-result truncation + elide-once + the
            # budget stop bound the tail without rewriting the cached prefix.)
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
            captured_reads=captured_reads,
        )


def _freeze_aged_tool_results(
    messages: list[dict[str, Any]],
    source_read_tools: frozenset[str],
    frozen: set[int],
) -> None:
    """Elide each aged TRANSIENT tool result exactly once, in place.

    Only navigation results (search_codebase, list_directory, …) are elided;
    they are consumed the moment the model picks its next target, so retaining
    them is waste. Source reads (``read_file``) are NEVER elided — they are the
    content decomposition reasons over (deduped at append time so a path is sent
    at most once).

    Mutating in place and eliding each message only once keeps every already-aged
    message byte-identical across rounds. That is the invariant the Anthropic
    rolling cache breakpoint depends on: any byte change inside the prefix
    invalidates the cache for everything after it. The most-recent assistant+tool
    block is left verbatim; it ages out (and is elided once) next round.
    """
    last_assistant_idx = None
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].get("role") == "assistant" and "tool_calls" in messages[i]:
            last_assistant_idx = i
            break

    if last_assistant_idx is None or last_assistant_idx <= 1:
        return

    for i in range(last_assistant_idx):
        if i in frozen:
            continue
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        tool_name = _find_tool_name(messages, msg.get("tool_call_id", "?"))
        if tool_name in source_read_tools:
            continue
        content = msg.get("content", "")
        line_count = (content.count("\n") + 1) if isinstance(content, str) else 0
        msg["content"] = (
            f"[{tool_name} output elided — {line_count} lines, navigation result not retained]"
        )
        frozen.add(i)


def _find_tool_name(messages: list[dict[str, Any]], tool_call_id: str) -> str:
    for msg in messages:
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                if tc.get("id") == tool_call_id:
                    return tc.get("function", {}).get("name", "unknown")
    return "unknown"


def _serialize_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments)


def _summarize_args(arguments: dict[str, Any]) -> str:
    parts = []
    for k, v in arguments.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)
