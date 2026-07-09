"""Recovery for malformed LLM assessment/decomposition output.

Owns the corrective re-prompt turns and the prose-misclassification heuristics
(largely glm-specific) that turn a silently-degraded response back into a parse
retry. The orchestrator delegates its recovery paths here so ``assess_task`` /
``evaluate_task`` stay focused on the happy-path control flow.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from core.domain.exceptions import InfeasibleError
from core.domain.services import parse_subtasks_from_llm_async
from core.domain.services.config_resolver import ConfigResolver


if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.services import AssessmentResult
    from core.domain.services.config_resolver import OperationType
    from core.domain.values.subtask import Subtask
    from core.ports.runtime_ports import FormatRepairerPort, LLMPort

logger = logging.getLogger(__name__)


_REAL_CONSTRAINT_FAILURE_KEYWORDS: tuple[str, ...] = (
    "constraints_unsatisfiable",
    "minimum_required",
    "constraint failure",
    "cannot satisfy",
    "cannot be satisfied",
)


def _looks_like_real_constraint_failure(content: str) -> bool:
    """Heuristic: did the LLM's *original* response declare a constraint failure?

    Used to distinguish a genuine ``constraints_unsatisfiable`` decision from
    a constraint failure synthesised by the format repairer when the actual
    response was unrelated prose. False negatives (real refusal worded
    creatively) are tolerable — they trigger one extra recovery turn that
    will simply re-surface the failure.
    """
    if not content:
        return False
    lowered = content.lower()
    return any(keyword in lowered for keyword in _REAL_CONSTRAINT_FAILURE_KEYWORDS)


# Conversational openings observed from glm-5.1:cloud when given a
# tools-enabled assessment/decomposition prompt. The model sometimes
# narrates its plan ("I'll start by…") instead of either issuing a tool
# call or emitting the required JSON. GPT, Claude, Qwen and DeepSeek do
# not exhibit this opening style, so detection here is a glm-specific
# guard — false positives on a well-behaved model would only trigger
# one extra LLM turn, never produce wrong decisions.
_GLM_PROSE_PREAMBLE_OPENERS: tuple[str, ...] = (
    "i'll ",
    "i will ",
    "let me ",
    "let's ",
    "first, i ",
    "first i ",
    "i need to ",
    "i'm going to ",
    "i am going to ",
    "to start, ",
    "to begin, ",
    "before decomposing",
    "before i decompose",
    "before producing",
    "sure, i'll ",
    "sure! i'll ",
    "i should ",
)


def _looks_like_glm_prose_preamble(content: str) -> bool:
    """Heuristic: does the response open with conversational planning prose?

    Detects the glm-5.1 failure mode where the model says "I'll start by
    researching…" instead of producing the required JSON or a structured
    tool call. Only fires when the trimmed content (a) opens with a known
    conversational phrase and (b) contains no JSON object/array delimiters
    in its first 200 characters — both must hold to avoid false positives
    on Qwen-style ``<think>I'll …</think>{...}`` outputs where the JSON is
    actually present.
    """
    if not content:
        return False
    trimmed = content.lstrip().lower()
    if not trimmed:
        return False
    # Strip leading <think>…</think> blocks that some Ollama models leak even
    # with think=False, since the JSON we care about lives after them.
    if trimmed.startswith("<think>"):
        end = trimmed.find("</think>")
        if end != -1:
            trimmed = trimmed[end + len("</think>") :].lstrip()
    if not trimmed.startswith(_GLM_PROSE_PREAMBLE_OPENERS):
        return False
    head = trimmed[:200]
    return "{" not in head and "[" not in head


# Phrases that betray decomposition intent inside an "execute" assessment's
# ``reasoning`` field. When the format repairer wraps glm prose in
# ``{"action": "execute", "reasoning": "<prose>"}`` the parse looks valid
# but the prose itself ("before decomposing", "let me read") contradicts
# the chosen action — the model meant to keep researching, not to execute
# as a single worker. Other models (GPT/Claude/Qwen/DeepSeek) phrase
# genuine ``execute`` decisions concretely ("single tool call sufficient",
# "task is atomic") and do not include these decomp-intent phrases.
_DECOMP_INTENT_REASONING_PHRASES: tuple[str, ...] = (
    "before decomposing",
    "before i decompose",
    "before decomposition",
    "i need to understand",
    "i need to investigate",
    "let me read",
    "let me research",
    "let me explore",
    "let me check",
    "let me first",
    "let me start by researching",
    "i'll start by researching",
    "i should research",
    "i should investigate",
    "i should first explore",
)


def _reasoning_contradicts_execute(reasoning: str) -> bool:
    """Detect glm prose surviving inside an assessment's ``reasoning`` field.

    glm-specific: only fires on phrases that imply the model intended to
    keep researching. GPT/Claude/Qwen/DeepSeek do not phrase ``execute``
    decisions this way, so this branch is functionally inert for them.
    """
    if not reasoning:
        return False
    lowered = reasoning.lower()
    return any(phrase in lowered for phrase in _DECOMP_INTENT_REASONING_PHRASES)


def _is_prose_misinterpreted_assessment(
    result: AssessmentResult,
    original_content: str,
) -> bool:
    """Detect a glm-style prose response silently parsed as ``execute``.

    Catches two variants of the same glm failure:

    1. **Raw prose**: the model emitted "I'll start by researching…" with
       no JSON; the format repairer fell back to ``{}`` which the parser
       defaulted to ``action="execute"``. Caught by
       :func:`_looks_like_glm_prose_preamble` against the raw content.
    2. **JSON-wrapped prose**: the model (or repairer) wrapped the prose
       in ``{"action": "execute", "reasoning": "I need to understand…
       before decomposing"}``. The parse looks structurally valid but
       the reasoning betrays decomposition intent. Caught by
       :func:`_reasoning_contradicts_execute` against the parsed reasoning.

    Either signal turns a misclassified WORKER decision back into a
    recovery turn. False positives only cost one extra LLM call.
    """
    if result.action != "execute":
        return False
    if result.subtasks:
        return False
    if _looks_like_glm_prose_preamble(original_content):
        return True
    return _reasoning_contradicts_execute(result.reasoning)


def build_retry_context_block(agent: AgentSession) -> str:
    """Assemble the retry failure-context appended to a worker prompt.

    Verification feedback (verifier rejection) and the failure digest (a
    crash/timeout of the prior attempt) are independent signals; each
    renders as its own block only when present, feedback first. Returns
    an empty string when neither is set.
    """
    blocks: list[str] = []
    if agent.verification_feedback:
        criteria_block = ""
        if agent.success_criteria:
            criteria_block = (
                f"\n\n**Success criteria you MUST satisfy:**\n{agent.success_criteria}\n"
            )
        blocks.append(
            "\n\n## Previous Attempt Feedback (Retry)\n"
            "Your previous attempt was rejected by the verifier:\n"
            f"> {agent.verification_feedback}\n"
            f"{criteria_block}\n"
            "You MUST address this feedback in your current attempt. "
            "Produce all required artifacts and evidence explicitly."
        )
    if agent.failure_digest:
        blocks.append(
            "\n\n## Previous Attempt Failure (Retry)\n"
            "Your previous attempt failed. Diagnostic digest of that attempt:\n"
            f"{agent.failure_digest}\n"
            "Address the cause above. Produce all required artifacts and evidence explicitly."
        )
    return "".join(blocks)


class AssessmentRecoveryService:
    """Corrective re-prompt turns for prose/malformed assessment output."""

    def __init__(
        self,
        llm_port: LLMPort,
        format_repairer: FormatRepairerPort | None = None,
    ) -> None:
        self._llm_port = llm_port
        self._format_repairer = format_repairer

    async def parse_decomposition_with_recovery(
        self,
        *,
        agent: AgentSession,
        response_content: str,
        original_prompt: str,
        op: OperationType,
    ) -> list[Subtask] | None:
        """Parse decomposition, with one corrective re-prompt on failure.

        Returns the parsed subtasks on success. Returns ``None`` when the
        agent has already been marked failed or infeasible — caller must
        not call ``fail_with_reason`` again. The recovery turn fires when:

        * the parser raised :class:`ValueError` (clearly malformed JSON), or
        * it raised :class:`InfeasibleError` but the *original* response
          contained no constraint-failure semantics — i.e. the format
          repairer mis-classified narrative prose as a refusal.

        A genuine ``constraints_unsatisfiable`` declaration from the model
        is honoured directly without recovery, since that is a deliberate
        semantic decision the orchestrator must respect.
        """
        try:
            return await parse_subtasks_from_llm_async(
                response_content,
                repairer=self._format_repairer,
            )
        except InfeasibleError as e:
            if _looks_like_real_constraint_failure(response_content):
                logger.warning(
                    "Agent %s reported unsatisfiable constraints: %s",
                    agent.agent_id,
                    e.failure.format_message(),
                )
                agent.mark_infeasible(
                    reason=e.failure.reason,
                    minimum_subtasks=e.failure.minimum_subtasks,
                    minimum_depth=e.failure.minimum_depth,
                )
                return None
            # Repairer synthesised a constraint failure from non-refusal
            # prose — same recovery path as a plain ValueError.
            parse_error: Exception = e
        except ValueError as e:
            parse_error = e

        recovered = await self.recover_decomposition_via_reprompt(
            agent=agent,
            original_prompt=original_prompt,
            narrative=response_content,
            op=op,
            parse_error=parse_error,
        )
        if recovered is None:
            agent.fail_with_reason(f"Failed to parse subtasks: {parse_error}")
            return None
        try:
            return await parse_subtasks_from_llm_async(
                recovered,
                repairer=self._format_repairer,
            )
        except InfeasibleError as e2:
            logger.warning(
                "Agent %s reported unsatisfiable constraints (after recovery turn): %s",
                agent.agent_id,
                e2.failure.format_message(),
            )
            agent.mark_infeasible(
                reason=e2.failure.reason,
                minimum_subtasks=e2.failure.minimum_subtasks,
                minimum_depth=e2.failure.minimum_depth,
            )
            return None
        except ValueError as e2:
            logger.warning(
                "Failed to parse subtasks for agent=%s after recovery turn: %s",
                agent.agent_id,
                e2,
            )
            agent.fail_with_reason(f"Failed to parse subtasks (after recovery): {e2}")
            return None

    async def recover_assessment_via_reprompt(
        self,
        *,
        agent: AgentSession,
        original_prompt: str,
        narrative: str,
        op: OperationType,
        parse_error: Exception | None,
    ) -> str | None:
        """One corrective LLM turn for a prose-style assessment response.

        Mirrors :meth:`recover_decomposition_via_reprompt` but targets the
        assessment schema (``action`` ∈ {execute, decompose}). Triggered
        from ``assess_task`` when the format-repaired output silently
        defaults to ``execute`` because the original response was a glm-
        style "I'll start by researching…" preamble. The corrective turn
        re-engages the same model with the original prompt as user, the
        prose as the assistant turn, and an explicit instruction to emit
        the assessment JSON object. Returns the new content on success,
        ``None`` if the recovery call itself failed.
        """
        if not narrative or not narrative.strip():
            return None

        logger.warning(
            "Assessment parse for agent=%s yielded prose / unparseable output "
            "(%s); attempting one corrective re-prompt against the same model",
            agent.agent_id,
            parse_error,
        )

        corrective = (
            "Your previous response was prose, not the required output. "
            "The output_format section of the original prompt requires a "
            "single JSON object with an ``action`` field set to either "
            '``"execute"`` (single worker) or ``"decompose"`` (with a '
            "``subtasks`` array), or a ``constraints_unsatisfiable`` object "
            "if and only if the limits truly cannot be met. No preamble, "
            "no commentary, no tool calls in this turn. Begin your "
            "response with `{` and end with `}`. Do not narrate."
        )
        messages = [
            {"role": "user", "content": original_prompt},
            {"role": "assistant", "content": narrative},
            {"role": "user", "content": corrective},
        ]
        llm_config = ConfigResolver.resolve(agent.config, operation=op)
        config_dict = llm_config.model_dump()
        if os.environ.get("ARISE_PRIME_MANAGER_CACHE") and agent.hierarchy_limits is not None:
            # manager-cache fix (env-gated): shared per-run OpenAI prompt-cache key so the
            # parallel phase managers route to one cache node that prime_manager_cache pre-warms.
            config_dict["prompt_cache_key"] = str(agent.hierarchy_limits.root_id)
        try:
            recovered = await self._llm_port.query_with_tools(
                messages=messages,
                config_dict=config_dict,
                tools=[],
            )
        except Exception as call_exc:
            logger.warning(
                "Assessment recovery call failed for agent=%s: %s",
                agent.agent_id,
                call_exc,
            )
            return None
        if recovered is None:
            logger.warning(
                "Assessment recovery call returned no response for agent=%s",
                agent.agent_id,
            )
            return None

        agent.emit_tokens_consumed(
            model=recovered.model,
            prompt_tokens=recovered.usage.prompt_tokens,
            completion_tokens=recovered.usage.completion_tokens,
            total_tokens=recovered.usage.total_tokens,
            cache_read_tokens=recovered.usage.cache_read_tokens,
            cache_write_tokens=recovered.usage.cache_write_tokens,
            cost_usd=recovered.cost_usd,
            operation=f"{op}_recovery",
        )
        return recovered.content or None

    async def recover_decomposition_via_reprompt(
        self,
        *,
        agent: AgentSession,
        original_prompt: str,
        narrative: str,
        op: OperationType,
        parse_error: Exception,
    ) -> str | None:
        """One corrective LLM turn after a narrative-instead-of-JSON response.

        Some models (notably glm-5.1 on a long tools-enabled prompt) reply with
        prose like "I'll start by researching the codebase…" instead of either
        a structured ``tool_call`` or the final JSON decomposition. The
        deterministic repairer + LLM format-repairer chain in
        :func:`parse_subtasks_from_llm_async` cannot rescue this, because the
        content is grammatical English with no JSON to repair. Instead we
        re-engage the same model in a second turn, quoting its prose and
        demanding JSON. Returns the new content on success, ``None`` if the
        recovery call itself failed.
        """
        if not narrative or not narrative.strip():
            return None

        logger.warning(
            "Decomposition parse failed for agent=%s (%s); attempting one "
            "corrective re-prompt against the same model",
            agent.agent_id,
            parse_error,
        )

        corrective = (
            "Your previous response was prose rather than the required output. "
            "The output_format section of the original prompt requires either "
            "(a) a JSON array of subtask objects, or (b) a "
            "``constraints_unsatisfiable`` JSON object — nothing else. No "
            "preamble, no commentary, no tool calls in this turn. Begin your "
            "response with `[` (subtasks) or `{` (constraint failure) and end "
            "with the matching closing bracket. Do not narrate."
        )
        messages = [
            {"role": "user", "content": original_prompt},
            {"role": "assistant", "content": narrative},
            {"role": "user", "content": corrective},
        ]
        llm_config = ConfigResolver.resolve(agent.config, operation=op)
        config_dict = llm_config.model_dump()
        if os.environ.get("ARISE_PRIME_MANAGER_CACHE") and agent.hierarchy_limits is not None:
            # manager-cache fix (env-gated): shared per-run OpenAI prompt-cache key so the
            # parallel phase managers route to one cache node that prime_manager_cache pre-warms.
            config_dict["prompt_cache_key"] = str(agent.hierarchy_limits.root_id)
        try:
            recovered = await self._llm_port.query_with_tools(
                messages=messages,
                config_dict=config_dict,
                tools=[],
            )
        except Exception as call_exc:
            logger.warning(
                "Decomposition recovery call failed for agent=%s: %s",
                agent.agent_id,
                call_exc,
            )
            return None
        if recovered is None:
            logger.warning(
                "Decomposition recovery call returned no response for agent=%s",
                agent.agent_id,
            )
            return None

        agent.emit_tokens_consumed(
            model=recovered.model,
            prompt_tokens=recovered.usage.prompt_tokens,
            completion_tokens=recovered.usage.completion_tokens,
            total_tokens=recovered.usage.total_tokens,
            cache_read_tokens=recovered.usage.cache_read_tokens,
            cache_write_tokens=recovered.usage.cache_write_tokens,
            cost_usd=recovered.cost_usd,
            operation=f"{op}_recovery",
        )
        return recovered.content or None
