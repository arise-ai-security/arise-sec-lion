"""Agent Orchestrator - coordinates LLM and worker interactions.

Handles the three orchestration operations as direct method calls:
1. Task assessment (PENDING -> WORKER or MANAGER+children in single call)
2. Task decomposition (BOSS/MANAGER -> children)
3. Worker execution (WORKER -> complete)
"""

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from core.application.services import (
    LLMQueryExecutor,
    SubtaskScope,
    ToolsetPolicyResolver,
    VerificationPipeline,
    build_prompt_capabilities,
)
from core.application.services.prompt.cache_breakpoint import strip_cache_breakpoint
from core.domain.exceptions import InfeasibleError, ToolNotAvailableError
from core.domain.services import (
    AssessmentResult,
    parse_assessment_response_async,
    parse_subtasks_from_llm_async,
)
from core.domain.services.config_resolver import ConfigResolver, OperationType
from core.domain.values.enums import AgentRole, AgentStatus


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


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.services import (
        ActiveToolContext,
        ChildAgentFactory,
        PromptBuilder,
    )
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.llm_response import LLMResponse
    from core.domain.values.node_message import Handoff
    from core.domain.values.subtask import Subtask
    from core.ports.runtime_ports import (
        FormatRepairerPort,
        LLMPort,
        RealtimeCallbackPort,
        WorkerToolPort,
    )
    from core.ports.shared_code_context_port import SharedCodeContextPort

logger = logging.getLogger(__name__)

_ASSESSMENT_PARSE_RETRIES = 2


class AgentOrchestrator:
    """Orchestrates agent interactions with LLM and worker tools.

    Three operations:
    - assess_task: PENDING agent → single LLM call decides execute (WORKER)
      or decompose (MANAGER+children)
    - evaluate_task: BOSS/MANAGER agent → LLM decomposes → spawn children
    - execute_task: WORKER agent → worker tool executes
    """

    def __init__(
        self,
        llm_port: "LLMPort",
        worker_port: "WorkerToolPort",
        prompt_builder: "PromptBuilder",
        child_factory: "ChildAgentFactory",
        realtime_callback: "RealtimeCallbackPort | None" = None,
        llm_query_executor: LLMQueryExecutor | None = None,
        toolset_resolver: ToolsetPolicyResolver | None = None,
        skip_judge: bool = False,
        format_repairer: "FormatRepairerPort | None" = None,
        shared_code_port: "SharedCodeContextPort | None" = None,
    ) -> None:
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder
        self._child_factory = child_factory
        self._realtime_callback = realtime_callback
        self._llm_query_executor = llm_query_executor or LLMQueryExecutor(llm_port)
        self._toolset_resolver = toolset_resolver or ToolsetPolicyResolver()
        self._format_repairer = format_repairer
        # When set, the shared code-prefix block (source upstream workers already
        # read, rebuilt from events) is injected into the worker prompt's stable
        # prefix. None => the block is never populated (byte-identical to today).
        self._shared_code_port = shared_code_port
        self._verification_pipeline = VerificationPipeline(
            llm_port,
            skip_judge=skip_judge,
            format_repairer=format_repairer,
        )

    async def assess_task(
        self,
        agent: "AgentSession",
        persist_checkpoint: "Callable[[AgentSession], Awaitable[object]] | None" = None,
    ) -> None:
        """Assess a PENDING agent: execute directly or decompose.

        Single LLM call replaces separate complexity evaluation + decomposition.
        On execute → WORKER. On decompose → MANAGER with children spawned.

        ``persist_checkpoint`` is called before the (potentially slow) LLM call so
        ``OperationStarted`` and ``PromptSent`` are durable in the event store
        even if the step is later cancelled by the watchdog.
        """
        if agent.role != AgentRole.PENDING:
            agent.fail_with_reason(f"Expected PENDING role, got {agent.role}")
            return

        op: OperationType = "task_assessment"
        with self._timed_operation(agent, op):
            try:
                # Shortcut: when the parent's decomposition flagged this
                # task as ``estimated_complexity="simple"``, the parent has
                # already scoped it to an atomic worker. Skip the assessment
                # LLM and execute directly. This is domain-agnostic — the
                # ``simple`` hint comes from whichever domain prompt drove
                # the decomposition (e.g. domain manager templates marking
                # atomic leaf workers as simple).
                # Open-source models otherwise re-decompose these into
                # duplicates of their own siblings, wasting tokens.
                if agent.estimated_complexity == "simple":
                    logger.info(
                        "Skipping assessment LLM for agent=%s — parent "
                        "decomposition marked it estimated_complexity=simple; "
                        "executing directly",
                        agent.agent_id,
                    )
                    await self._apply_assessment_result(
                        agent,
                        AssessmentResult(
                            action="execute",
                            reasoning=(
                                "Parent decomposition marked this task "
                                "estimated_complexity=simple; assessment "
                                "LLM skipped"
                            ),
                        ),
                    )
                    return

                domain_context = self._get_domain_context(agent)
                tool_context = self._toolset_resolver.resolve(agent.role)

                scope = self._build_scope(agent)
                prompt = self._prompt_builder.build_assessment_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    briefing=agent.briefing,
                    hierarchy_limits=agent.hierarchy_limits,
                    domain_context=domain_context,
                    scope=scope,
                    prompt_capabilities=build_prompt_capabilities(tool_context),
                )
                agent.emit_prompt_sent(
                    prompt=strip_cache_breakpoint(prompt), prompt_type=op, target="llm"
                )

                # Flush the prompt envelope before the LLM call so the DB shows
                # the agent IS doing work — without this the orchestrator looks
                # silent for the full LLM round-trip (which can be 5+ minutes for
                # recon-heavy prompts on Sonnet/Opus).
                if persist_checkpoint is not None:
                    await persist_checkpoint(agent)

                # Query LLM and parse; retry on malformed JSON. After the
                # retry loop exhausts (or a glm-style prose preamble was
                # silently turned into a default ``execute`` decision by the
                # format repairer), do one corrective re-prompt against the
                # same model — see ``_recover_assessment_via_reprompt``.
                last_parse_error: Exception | None = None
                last_response_content = ""
                result = None
                for attempt in range(_ASSESSMENT_PARSE_RETRIES):
                    response = await self._query_llm(
                        agent,
                        prompt,
                        op,
                        tool_context=tool_context,
                    )
                    last_response_content = response.content
                    try:
                        candidate = await parse_assessment_response_async(
                            response.content,
                            repairer=self._format_repairer,
                        )
                    except (json.JSONDecodeError, ValueError, KeyError) as e:
                        last_parse_error = e
                        # If the original content was glm-style prose, retrying
                        # the same prompt is wasted spend — the failure is
                        # deterministic. Skip directly to the recovery turn.
                        # For other parse errors (malformed JSON), retry as
                        # before — they may be stochastic (e.g. truncation).
                        if _looks_like_glm_prose_preamble(response.content):
                            logger.warning(
                                "Assessment attempt %d/%d for agent=%s "
                                "produced a prose preamble; skipping remaining "
                                "retries and going straight to recovery turn "
                                "(glm quirk)",
                                attempt + 1,
                                _ASSESSMENT_PARSE_RETRIES,
                                agent.agent_id,
                            )
                            break
                        if attempt < _ASSESSMENT_PARSE_RETRIES - 1:
                            logger.warning(
                                "Assessment parse attempt %d/%d failed for agent=%s: %s",
                                attempt + 1,
                                _ASSESSMENT_PARSE_RETRIES,
                                agent.agent_id,
                                e,
                            )
                        continue

                    if _is_prose_misinterpreted_assessment(
                        candidate,
                        response.content,
                    ):
                        # GLM-specific failure mode: model emits "I'll start by
                        # researching…" prose; format repairer transforms it
                        # into ``{}`` which the parser defaults to
                        # ``action="execute"``. That silently turns a
                        # decomposition target into a single WORKER. Treat as
                        # a parse failure and skip remaining retries — the
                        # prose is deterministic per-attempt, so the only
                        # productive next step is the corrective re-prompt.
                        # GPT/Qwen/DeepSeek never emit this preamble, so this
                        # branch is glm-only.
                        last_parse_error = ValueError("Prose preamble misclassified as 'execute'")
                        logger.warning(
                            "Assessment attempt %d/%d for agent=%s produced a "
                            "prose-style preamble (parsed as 'execute'); "
                            "skipping remaining retries (glm quirk)",
                            attempt + 1,
                            _ASSESSMENT_PARSE_RETRIES,
                            agent.agent_id,
                        )
                        break

                    result = candidate
                    break

                if result is None:
                    recovered_content = await self._recover_assessment_via_reprompt(
                        agent=agent,
                        original_prompt=prompt,
                        narrative=last_response_content,
                        op=op,
                        parse_error=last_parse_error,
                    )
                    if recovered_content is not None:
                        try:
                            result = await parse_assessment_response_async(
                                recovered_content,
                                repairer=self._format_repairer,
                            )
                        except (json.JSONDecodeError, ValueError, KeyError) as e2:
                            # Recovery turn produced empty / malformed
                            # content too. Rather than killing the agent —
                            # which orphans its sibling deliverables —
                            # fall back to single-worker execution. The
                            # parent already broke this task down; even a
                            # leaf attempt is more useful than a hard fail.
                            logger.warning(
                                "Recovery-turn parse failed for agent=%s "
                                "(%s); defaulting to single-worker execute",
                                agent.agent_id,
                                e2,
                            )
                            result = AssessmentResult(
                                action="execute",
                                reasoning=(
                                    "Assessment failed after recovery turn; "
                                    "defaulting to single-worker execute"
                                ),
                            )
                    if result is None:
                        # Recovery call itself failed (network/auth/empty
                        # content). Same fallback rationale as above.
                        logger.warning(
                            "Assessment recovery unavailable for agent=%s "
                            "(last error: %s); defaulting to single-worker "
                            "execute",
                            agent.agent_id,
                            last_parse_error,
                        )
                        result = AssessmentResult(
                            action="execute",
                            reasoning=(
                                "Assessment LLM unavailable after retries "
                                "and recovery turn; defaulting to "
                                "single-worker execute"
                            ),
                        )

                await self._apply_assessment_result(agent, result)
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                agent.fail_with_reason(f"Assessment failed: {e}")

    async def evaluate_task(
        self,
        agent: "AgentSession",
        persist_checkpoint: "Callable[[AgentSession], Awaitable[object]] | None" = None,
    ) -> None:
        """Decompose task into subtasks for a BOSS/MANAGER agent.

        LLM generates subtasks, limits are enforced, children are spawned.
        On failure, agent is failed with reason.

        ``persist_checkpoint`` is called before the LLM call so ``OperationStarted``
        and ``PromptSent`` are durable in the event store even if the step is
        later cancelled. Without this, recon-heavy decomposition prompts produce
        4-5 minute silent gaps in the event store.
        """
        if agent.role not in (AgentRole.BOSS, AgentRole.MANAGER):
            agent.fail_with_reason(f"Expected BOSS/MANAGER role, got {agent.role}")
            return

        op: OperationType = "task_decomposition"
        with self._timed_operation(agent, op):
            domain_context = self._get_domain_context(agent)
            tool_context = self._toolset_resolver.resolve(agent.role)

            # Build role-specific prompt (with recon tool capabilities if available)
            capabilities = build_prompt_capabilities(tool_context)
            if agent.role == AgentRole.BOSS:
                prompt = self._prompt_builder.build_boss_delegation_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    domain_context=domain_context,
                    hierarchy_limits=agent.hierarchy_limits,
                    prompt_capabilities=capabilities,
                )
            else:
                scope = self._build_scope(agent)
                prompt = self._prompt_builder.build_manager_decomposition_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    domain_context=domain_context,
                    briefing=agent.briefing,
                    hierarchy_limits=agent.hierarchy_limits,
                    scope=scope,
                    prompt_capabilities=capabilities,
                )
            agent.emit_prompt_sent(
                prompt=strip_cache_breakpoint(prompt), prompt_type=op, target="llm"
            )

            # Flush prompt envelope before the LLM call so the event store
            # shows the boss/manager IS doing work — without this the recon
            # round-trip is invisible for minutes (see acf44cbc 4m48s gap).
            if persist_checkpoint is not None:
                await persist_checkpoint(agent)

            # Query LLM (with tool calling if available)
            response = await self._query_llm(
                agent,
                prompt,
                op,
                tool_context=tool_context,
            )

            # Parse subtasks. On parse failure, do one corrective re-prompt
            # against the same model: some models (e.g. glm-5.1 on a long
            # tools-enabled prompt) emit prose ("I'll start by researching…")
            # instead of a structured tool call or the final JSON array.
            subtasks = await self._parse_decomposition_with_recovery(
                agent=agent,
                response_content=response.content,
                original_prompt=prompt,
                op=op,
            )
            if subtasks is None:
                # Already failed/marked-infeasible by helper.
                return

            failure = await self._spawn_children(agent, subtasks)
            if failure:
                agent.fail_with_reason(failure)
                return

    async def execute_task(
        self,
        agent: "AgentSession",
        working_directory: str | None = None,
        workspace_context: str | None = None,
        handoff: "Handoff | None" = None,
        task_context_overrides: dict[str, Any] | None = None,
        persist_checkpoint: "Callable[[AgentSession], Awaitable[object]] | None" = None,
    ) -> None:
        """Execute task for a WORKER agent using worker tool.

        On failure, agent is failed with reason.

        When ``persist_checkpoint`` is provided, it is awaited after each worker
        event is applied. This flushes in-flight events to the store so that a
        watchdog cancellation does not discard the worker's trace (the orchestrator
        otherwise persists only at end-of-step).
        """
        if agent.role != AgentRole.WORKER:
            agent.fail_with_reason(f"Expected WORKER role, got {agent.role}")
            return

        op = "worker_execution"
        with self._timed_operation(agent, op):
            # Start worker execution (CodeGenerationStarted event, -> IN_PROGRESS)
            tool_name = agent.config.tool
            agent.start_worker_execution(tool_name)

            root_id = agent.hierarchy_limits.root_id if agent.hierarchy_limits else None
            shared_code_block = await self._resolve_shared_code_block(root_id)

            prompt = self._prompt_builder.build_worker_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                handoff=handoff,
                workspace_context=workspace_context,
                domain_context=self._get_domain_context(agent),
                briefing=agent.briefing,
                shared_code_block=shared_code_block,
            )

            # Inject verification feedback on retry so worker knows what to fix
            if agent.verification_feedback and agent.retry_count > 0:
                criteria_block = ""
                if agent.success_criteria:
                    criteria_block = (
                        f"\n\n**Success criteria you MUST satisfy:**\n{agent.success_criteria}\n"
                    )
                prompt += (
                    "\n\n## Previous Attempt Feedback (Retry)\n"
                    "Your previous attempt was rejected by the verifier:\n"
                    f"> {agent.verification_feedback}\n"
                    f"{criteria_block}\n"
                    "You MUST address this feedback in your current attempt. "
                    "Produce all required artifacts and evidence explicitly."
                )

            agent.emit_prompt_sent(prompt=prompt, prompt_type=op, target=tool_name)

            # Flush the start/prompt envelope before entering the worker stream
            # so the trace is preserved even if the first SDK event never lands
            # (e.g., LLM hangs before first token) and the watchdog cancels us.
            if persist_checkpoint is not None:
                await persist_checkpoint(agent)

            # Run worker session
            task_context: dict[str, Any] = {
                "agent_id": agent.agent_id,
                "task_description": prompt,
                "tool_name": tool_name,
                "config": agent.config,
            }
            if working_directory:
                task_context["working_directory"] = working_directory
            if root_id is not None:
                # Scope for the worker's shared-code capture (root of this run).
                task_context["root_id"] = root_id
            if task_context_overrides:
                task_context.update(task_context_overrides)

            try:
                async for tool_event in self._worker_port.run_session(task_context):
                    agent.apply_worker_event(tool_event)
                    if persist_checkpoint is not None:
                        await persist_checkpoint(agent)
                    if self._realtime_callback and root_id:
                        await self._realtime_callback.on_event(tool_event, root_id)
            except ToolNotAvailableError as e:
                agent.fail_with_reason(str(e))
                return

            # Verify worker output if completed successfully
            if agent.status == AgentStatus.COMPLETED:
                await self._verification_pipeline.verify(agent)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @contextmanager
    def _timed_operation(
        self,
        agent: "AgentSession",
        operation_type: str,
    ) -> Iterator[None]:
        """Emit start/finish telemetry around an operation."""
        start_time = time.monotonic()
        agent.emit_operation_started(operation_type=operation_type)
        try:
            yield
        finally:
            duration = time.monotonic() - start_time
            agent.emit_operation_finished(
                operation_type=operation_type,
                duration_seconds=duration,
            )

    async def _check_limit_violations(self, agent: "AgentSession", subtasks: list) -> str | None:
        """Check hard limits. Returns failure reason or None.

        Async to compose with the atomic reservation path in
        ``_spawn_children``; the cap check itself is sync but the
        cascade keeps the call chain awaitable end-to-end (D.2).
        """
        limits = agent.hierarchy_limits
        violations: list[dict[str, Any]] = []

        # Hard limit: children per node
        if (
            limits is not None
            and limits.is_children_limited()
            and len(subtasks) > limits.max_children_per_node
        ):
            violations.append(
                {
                    "limit_type": "children",
                    "limit_value": limits.max_children_per_node,
                    "attempted_value": len(subtasks),
                    "action_taken": "agent_failed",
                }
            )

        # Hard limit: total agents
        max_total = self._child_factory.max_total_agents
        if max_total > 0:
            current_total = self._child_factory.total_created
            remaining = max_total - current_total
            if len(subtasks) > remaining:
                violations.append(
                    {
                        "limit_type": "total_agents",
                        "limit_value": max_total,
                        "attempted_value": current_total + len(subtasks),
                        "action_taken": "agent_failed",
                    }
                )

        if violations:
            for v in violations:
                agent.emit_limit_enforced(**v)
            types = [v["limit_type"] for v in violations]
            return f"LLM violated limits: {types}"

        return None

    async def _spawn_children(
        self,
        agent: "AgentSession",
        subtasks: list["Subtask"],
    ) -> str | None:
        """Validate limits, determine child role, and spawn children.

        Async (D.2): after the optimistic sync cap check, atomically
        reserves ``len(subtasks)`` slots against the factory so concurrent
        MANAGER evaluations cannot oversubscribe ``max_total_agents``.
        On reservation failure emits ``LimitEnforced`` and aborts.
        """
        failure = await self._check_limit_violations(agent, subtasks)
        if failure:
            return failure

        # --- Cross-tree role dedup -------------------------------------------
        # Prevent managers from re-decomposing into roles that already exist
        # as their own siblings (uncle duplication).  Prescribed top-level
        # roles (Builder, Exploiter, Fixer, Reporter) are always allowed;
        # additional / generated roles must be unique across branches.
        subtasks = self._dedup_subtasks_against_siblings(agent, subtasks)
        if not subtasks:
            # Every proposed subtask was a duplicate → force direct execution
            logger.warning(
                "Agent %s: all subtasks duplicate sibling roles — forcing direct execution",
                agent.agent_id,
            )
            agent.apply_complexity_result(
                complexity="simple",
                reasoning="All proposed subtasks duplicate sibling roles; executing directly",
                determined_role=AgentRole.WORKER,
            )
            return None
        # ---------------------------------------------------------------------

        # Atomic reservation: protects against concurrent MANAGER spawn
        # races where both callers passed the stale-count cap check.
        max_total = self._child_factory.max_total_agents
        if max_total > 0:
            reserved_n = len(subtasks)
            reserved = await self._child_factory.try_reserve(reserved_n)
            if not reserved:
                agent.emit_limit_enforced(
                    limit_type="total_agents",
                    limit_value=max_total,
                    attempted_value=reserved_n,
                    action_taken="agent_failed",
                )
                return "reservation_failed:total_agents"
            # Stash on the aggregate so the OCC retry handler in
            # ExecutionService can release the reservation before reload.
            agent.pending_reservation = reserved_n

        limits = agent.hierarchy_limits
        force_worker = False
        if limits is not None and not limits.can_spawn_child():
            force_worker = True
            agent.emit_limit_enforced(
                limit_type="depth",
                limit_value=limits.max_depth,
                attempted_value=limits.current_depth + 1,
                action_taken="forced_worker_role",
            )

        child_role = AgentRole.WORKER.value if force_worker else AgentRole.PENDING.value

        agent.apply_subtasks_and_spawn_children(
            subtasks=subtasks,
            child_role=child_role,
            briefing=agent.build_briefing_for_child(),
        )
        return None

    # Role-prefix pattern: "[Build-Setup]" → "Build-Setup"
    _BRACKET_PREFIX = re.compile(r"^\[([^\]]+)\]")

    @classmethod
    def _extract_role_prefix(cls, description: str) -> str | None:
        """Extract [Bracketed-Role-Name] from a task description."""
        m = cls._BRACKET_PREFIX.match(description.strip())
        return m.group(1) if m else None

    def _dedup_subtasks_against_siblings(
        self,
        agent: "AgentSession",
        subtasks: list["Subtask"],
    ) -> list["Subtask"]:
        """Remove subtasks whose role names duplicate the agent's own siblings.

        When a manager re-decomposes into the exact same roles as its
        siblings, all those workers are redundant — the siblings already
        cover them.  This is a common Qwen failure mode.

        Returns:
            Filtered subtask list (may be empty).
        """
        if agent.parent_id is None:
            return subtasks  # Boss-level decomposition — no siblings to clash with

        # Collect uncle role names (siblings of the current agent)
        uncle_roles = self._get_sibling_role_prefixes(agent)

        # Collect tree-wide role names to catch cross-subtree duplication
        tree_roles = self._get_tree_role_prefixes(agent)

        if not uncle_roles and not tree_roles:
            return subtasks  # No siblings or role info — skip check

        # Also include the agent's own role as off-limits for children
        own_role = self._extract_role_prefix(agent.task_description or "")
        forbidden = uncle_roles | tree_roles | ({own_role} if own_role else set())

        kept: list["Subtask"] = []
        for st in subtasks:
            prefix = self._extract_role_prefix(st.description)
            if prefix and prefix in forbidden:
                logger.info(
                    "Agent %s: stripping duplicate subtask [%s] (already covered by sibling/self)",
                    agent.agent_id,
                    prefix,
                )
                continue
            kept.append(st)

        if kept and len(kept) < len(subtasks):
            logger.info(
                "Agent %s: kept %d/%d subtasks after dedup",
                agent.agent_id,
                len(kept),
                len(subtasks),
            )

        return kept

    def _get_sibling_role_prefixes(self, agent: "AgentSession") -> set[str]:
        """Get [Role-Name] prefixes of the agent's siblings (same parent)."""
        if agent.parent_id is None:
            return set()
        registry = getattr(self._child_factory, "_limits_registry", None)
        if registry is None:
            return set()
        return registry.get_sibling_role_prefixes(agent.agent_id, agent.parent_id)

    def _get_tree_role_prefixes(self, agent: "AgentSession") -> set[str]:
        """Get ALL [Role-Name] prefixes used anywhere in the execution tree."""
        registry = getattr(self._child_factory, "_limits_registry", None)
        if registry is None:
            return set()
        return registry.get_tree_role_prefixes(agent.agent_id)

    async def _apply_assessment_result(
        self,
        agent: "AgentSession",
        result: AssessmentResult,
    ) -> None:
        """Apply the parsed assessment outcome to the agent.

        Async (D.2): the decompose path awaits ``_spawn_children``, which
        in turn awaits ``ChildAgentFactory.try_reserve``. Keeping this
        method sync would silently truthy-check the returned coroutine
        as ``failure_msg`` and never await it.
        """
        if result.action == "infeasible":
            if result.constraint_failure is None:
                agent.fail_with_reason("Assessment reported infeasible but no failure details")
                return
            agent.mark_infeasible(
                reason=result.constraint_failure.reason,
                minimum_subtasks=result.constraint_failure.minimum_subtasks,
                minimum_depth=result.constraint_failure.minimum_depth,
            )
            return

        if result.action == "execute":
            agent.apply_complexity_result(
                complexity="simple",
                reasoning=result.reasoning,
                determined_role=AgentRole.WORKER,
            )
            return

        agent.apply_complexity_result(
            complexity="complex",
            reasoning=result.reasoning,
            determined_role=AgentRole.MANAGER,
        )
        if result.subtasks is None:
            agent.fail_with_reason("Assessment requested decomposition without subtasks")
            return

        failure_msg = await self._spawn_children(agent, result.subtasks)
        if failure_msg:
            agent.fail_with_reason(failure_msg)

    async def _query_llm(
        self,
        agent: "AgentSession",
        prompt: str,
        operation: OperationType,
        tool_context: "ActiveToolContext | None" = None,
    ) -> "LLMResponse":
        """Query the LLM with optional tool-calling context.

        Dispatches through LLMQueryExecutor, emits probe events for tool calls,
        and records the aggregated token usage on the agent.

        Returns:
            The final LLMResponse (aggregated across all tool-calling turns).
        """
        llm_config = ConfigResolver.resolve(agent.config, operation=operation)
        config_dict = llm_config.model_dump()
        result = await self._llm_query_executor.query(
            prompt,
            config_dict,
            tool_context=tool_context,
        )
        response = result.response
        self._emit_probe_events(agent, result.tool_records)

        agent.emit_tokens_consumed(
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            cost_usd=response.cost_usd,
            operation=operation,
        )

        return response

    async def _parse_decomposition_with_recovery(
        self,
        *,
        agent: "AgentSession",
        response_content: str,
        original_prompt: str,
        op: OperationType,
    ) -> "list[Subtask] | None":
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

        recovered = await self._recover_decomposition_via_reprompt(
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

    async def _recover_assessment_via_reprompt(
        self,
        *,
        agent: "AgentSession",
        original_prompt: str,
        narrative: str,
        op: OperationType,
        parse_error: Exception | None,
    ) -> str | None:
        """One corrective LLM turn for a prose-style assessment response.

        Mirrors :meth:`_recover_decomposition_via_reprompt` but targets the
        assessment schema (``action`` ∈ {execute, decompose}). Triggered
        from :meth:`assess_task` when the format-repaired output silently
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

    async def _recover_decomposition_via_reprompt(
        self,
        *,
        agent: "AgentSession",
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

    @staticmethod
    def _get_domain_context(agent: "AgentSession") -> object | None:
        if agent.hierarchy_limits:
            return agent.hierarchy_limits.domain_context
        return None

    async def _resolve_shared_code_block(self, root_id: "UUID | None") -> str | None:
        """The run's shared code-prefix block, or None when the feature is off.

        Rebuilt from the event store by the provider; gated by the presence of the
        port (only wired when ``orchestration.shared_worker_session`` is on) and a
        known run scope.
        """
        if self._shared_code_port is None or root_id is None:
            return None
        return await self._shared_code_port.code_block(root_id)

    @staticmethod
    def _build_scope(agent: "AgentSession") -> SubtaskScope | None:
        if not (agent.target_paths or agent.symbols or agent.search_hints):
            return None
        return SubtaskScope(
            target_paths=agent.target_paths,
            symbols=agent.symbols,
            search_hints=agent.search_hints,
        )

    @staticmethod
    def _emit_probe_events(
        agent: "AgentSession",
        tool_records: list,
    ) -> None:
        for record in tool_records:
            agent.emit_probe_started(probe_type=record.tool_name)
            agent.emit_probe_completed(
                probe_type=record.tool_name,
                result_summary=record.result_summary,
            )
