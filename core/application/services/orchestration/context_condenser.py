"""Context condensation for the recon tool-calling loop.

Prevents context window overflow by applying three layers:
1. Per-result hard cap — truncate each tool output before it enters history
2. Sliding-window summarization — compress older tool exchanges into a digest
3. Token budget gate — estimate total tokens and force-condense when near limit

Inspired by the context management strategies used in Claude Code
(auto-compression of older messages) and OpenHands (LLMSummarizingCondenser,
AmortizedForgettingCondenser).
"""

import logging
from typing import TYPE_CHECKING, Any

from core.ports.runtime_ports import LLMPort


if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.llm_response import LLMResponse

logger = logging.getLogger(__name__)

# Rough estimate: 1 token ≈ 4 chars for English/code text.
_CHARS_PER_TOKEN = 4

# Default summarization model — fast and cheap.
_CONDENSER_MODEL = "gpt-4o-mini"

_CONDENSE_SYSTEM = (
    "You are a context compressor. Summarize the tool-calling exchange below "
    "into a concise digest of FINDINGS ONLY. Keep file paths, function names, "
    "key line numbers, and structural insights. Drop raw file contents and "
    "verbose grep output. Output a single markdown block, max 300 words."
)


class ContextCondenser:
    """Manages context budget for the recon tool-calling loop.

    Three modes of operation (configured independently):

    - ``result_char_limit``: Hard cap on each individual tool result.
    - ``condense_after_iteration``: After this many iterations, older tool
      exchanges are LLM-summarized into a compact digest.
    - ``token_budget``: Estimated token ceiling; if exceeded, older messages
      are force-condensed before the next LLM call.
    """

    def __init__(
        self,
        llm_port: LLMPort | None = None,
        *,
        result_char_limit: int = 6_000,
        condense_after_iteration: int = 2,
        token_budget: int = 80_000,
        condenser_model: str = _CONDENSER_MODEL,
    ) -> None:
        self._llm_port = llm_port
        self._result_char_limit = result_char_limit
        self._condense_after = condense_after_iteration
        self._token_budget = token_budget
        self._condenser_model = condenser_model

    # ── Layer 1: per-result truncation ──────────────────────────────────

    def truncate_result(self, result: str, *, char_limit: int | None = None) -> str:
        """Hard-cap a single tool result.

        Args:
            result: Raw tool output.
            char_limit: Override for the instance default. Passed from
                ``ReconPolicy.result_char_limit`` when available.
        """
        limit = char_limit if char_limit is not None else self._result_char_limit
        if len(result) <= limit:
            return result
        half = limit // 2
        head = result[:half]
        tail = result[-half:]
        omitted = len(result) - limit
        return (
            f"{head}\n\n[...{omitted} chars omitted — "
            f"showing first and last {half} chars...]\n\n{tail}"
        )

    # ── Layer 2: sliding-window condensation ────────────────────────────

    async def maybe_condense(
        self,
        messages: list[dict[str, Any]],
        current_iteration: int,
        *,
        agent: "AgentSession | None" = None,
    ) -> list[dict[str, Any]]:
        """Condense older tool exchanges if we've passed the threshold.

        Replaces assistant+tool message pairs from earlier iterations with
        a single ``[RECON SUMMARY]`` user message containing a compressed
        digest. The most recent iteration's messages are kept verbatim so
        the LLM can reference exact file contents it just read.

        When ``agent`` is supplied, the LLM summarization call's token usage
        is recorded via ``TokensConsumed(operation='context_condense')``.

        Returns a new list (does not mutate the input).
        """
        if current_iteration < self._condense_after:
            return messages

        # Find the boundary: keep the original prompt (messages[0]) and the
        # messages from the *latest* iteration.  Everything in between is
        # eligible for condensation.
        #
        # Message structure per iteration:
        #   {"role": "assistant", "tool_calls": [...]}
        #   {"role": "tool", ...}  (one per tool call)
        #
        # The latest iteration's messages are at the tail.  Walk backwards
        # to find where the last assistant+tool block starts.

        # Find the start of the most recent assistant+tool block
        last_assistant_idx = None
        for i in range(len(messages) - 1, 0, -1):
            if messages[i].get("role") == "assistant" and "tool_calls" in messages[i]:
                last_assistant_idx = i
                break

        if last_assistant_idx is None or last_assistant_idx <= 1:
            # No prior tool exchanges to condense
            return messages

        # Segment: [prompt] [old tool exchanges] [latest tool exchange]
        prompt_msg = messages[0]
        old_messages = messages[1:last_assistant_idx]
        recent_messages = messages[last_assistant_idx:]

        if not old_messages:
            return messages

        summary = await self._summarize_messages(old_messages, agent=agent)

        condensed = [
            prompt_msg,
            {
                "role": "user",
                "content": (
                    f"[RECON SUMMARY — condensed from {len(old_messages)} earlier messages]\n\n"
                    f"{summary}"
                ),
            },
            *recent_messages,
        ]

        old_tokens = self._estimate_tokens(old_messages)
        new_tokens = self._estimate_tokens([condensed[1]])
        logger.info(
            "Condensed %d old messages: ~%d tokens → ~%d tokens (%.0f%% reduction)",
            len(old_messages),
            old_tokens,
            new_tokens,
            (1 - new_tokens / max(old_tokens, 1)) * 100,
        )

        return condensed

    async def _summarize_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        agent: "AgentSession | None" = None,
    ) -> str:
        """Produce a compact summary of tool exchanges.

        If an LLM port is available, uses a cheap model for summarization and
        records the call's token usage via ``agent.emit_tokens_consumed`` when
        an agent is supplied. Otherwise, falls back to deterministic extraction
        of key lines. The deterministic fallback never emits ``TokensConsumed``.
        """
        # Build a text representation of the tool exchanges
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    parts.append(f"Called: {fn.get('name', '?')}({fn.get('arguments', '')})")
            elif role == "tool":
                content = msg.get("content", "")
                # Include a shortened version for the summarizer
                if len(content) > 2000:
                    content = content[:1000] + "\n[...]\n" + content[-500:]
                parts.append(f"Result:\n{content}")

        exchange_text = "\n\n".join(parts)

        if self._llm_port is not None:
            try:
                summary_prompt = (
                    f"{_CONDENSE_SYSTEM}\n\n"
                    f"---\n{exchange_text}\n---\n\n"
                    "Concise digest of findings:"
                )
                response = await self._llm_port.query_with_usage(
                    summary_prompt,
                    {"model": self._condenser_model, "temperature": 0.0, "max_tokens": 800},
                )
                if agent is not None:
                    self._emit_condense_tokens(agent, response)
                return response.content
            except Exception:
                logger.warning("LLM summarization failed, using deterministic fallback")

        # Deterministic fallback: extract tool names + first line of each result
        return self._deterministic_summary(messages)

    @staticmethod
    def _emit_condense_tokens(agent: "AgentSession", response: "LLMResponse") -> None:
        """Record the condenser LLM call's token usage on the agent's event stream."""
        agent.emit_tokens_consumed(
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cost_usd=response.cost_usd,
            operation="context_condense",
        )

    @staticmethod
    def _deterministic_summary(messages: list[dict[str, Any]]) -> str:
        """Extract tool call names and truncated results without LLM."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    lines.append(f"- {fn.get('name', '?')}({fn.get('arguments', '')})")
            elif role == "tool":
                content = msg.get("content", "")
                # Keep first 3 lines as a preview
                preview = "\n".join(content.splitlines()[:3])
                if len(content.splitlines()) > 3:
                    preview += f"\n  [...{len(content.splitlines())} lines total]"
                lines.append(f"  → {preview}")
        return "\n".join(lines)

    # ── Layer 3: token budget gate ──────────────────────────────────────

    def estimate_total_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimate for the full message list."""
        return self._estimate_tokens(messages)

    def exceeds_budget(self, messages: list[dict[str, Any]]) -> bool:
        """Check if estimated tokens exceed the configured budget."""
        return self._estimate_tokens(messages) > self._token_budget

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate token count from message char lengths."""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            # Count tool_calls arguments
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                total_chars += len(fn.get("arguments", ""))
                total_chars += len(fn.get("name", ""))
        return total_chars // _CHARS_PER_TOKEN
