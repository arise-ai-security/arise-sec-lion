"""Deterministic failure digest built from a crashed worker's aggregate state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.services.orchestration.truncation import head_tail


if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession


_DIGEST_CHAR_CAP = 4000


def build_failure_digest(agent: AgentSession) -> str:
    """Build a bounded, deterministic digest of a failed worker attempt.

    Pure function of replayed aggregate state (error message, retained tool-output
    tail, retry count): no LLM, no filesystem, no logging. The same agent state
    always yields the same digest, so it is safe to carry in an event and inject
    into the retry prompt and the parent's failure record.
    """
    lines = [f"FAILURE: {agent.error_message or 'unknown'}", "LAST TOOL CALLS:"]
    if agent.recent_thoughts:
        for thought in agent.recent_thoughts:
            tool = thought.tool_name or "-"
            lines.append(f"[{tool}/{thought.output_type}] {thought.content}")
    else:
        lines.append("(no tool output captured)")
    lines.append(f"ATTEMPT: {agent.retry_count + 1}")

    return head_tail("\n".join(lines), _DIGEST_CHAR_CAP)
