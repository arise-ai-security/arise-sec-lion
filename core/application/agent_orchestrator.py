"""Agent Orchestrator - coordinates LLM and worker interactions.

Handles the three orchestration operations as direct method calls:
1. Task assessment (PENDING -> WORKER or MANAGER+children in single call)
2. Task decomposition (BOSS/MANAGER -> children)
3. Worker execution (WORKER -> complete)
"""

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.application.services import (
    LLMQueryExecutor,
    SubtaskScope,
    ToolsetPolicyResolver,
    VerificationPipeline,
    build_prompt_capabilities,
)
from core.application.services.orchestration.assessment_recovery import (
    AssessmentRecoveryService,
    _is_prose_misinterpreted_assessment,
    _looks_like_glm_prose_preamble,
    build_retry_context_block,
)
from core.application.services.orchestration.decomposition_contract import (
    DecompositionContractService,
)
from core.application.services.orchestration.recon_propagation import ReconPropagationService
from core.application.services.prompt.cache_breakpoint import strip_cache_breakpoint
from core.domain.exceptions import ToolNotAvailableError
from core.domain.services import AssessmentResult, parse_assessment_response_async
from core.domain.services.config_resolver import ConfigResolver, OperationType
from core.domain.values.enums import AgentRole, AgentStatus
from core.domain.values.procedure import ProcedureResult
from core.ports.procedure_ports import NullProcedureExecutor


# manager-cache prime: OpenAI serves a cached entry only when it is a complete prefix
# (messages + tool defs) of the new request, in >=1024-token / ~4 KB increments. Sibling
# managers share a long head but diverge mid-prompt, so we pre-warm their longest common
# prefix once. Below this floor the prime cannot pay for itself, so skip it.
_MIN_PRIME_PREFIX_CHARS = 4096
_PRIME_MAX_TOKENS = 16


def _longest_common_prefix(strings: list[str]) -> str:
    """Longest byte-identical leading substring shared by every string ("" if none)."""
    if not strings:
        return ""
    lo = min(strings)
    hi = max(strings)
    for i, ch in enumerate(lo):
        if ch != hi[i]:
            return lo[:i]
    return lo


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
    from core.ports.decomposition_validator_port import DecompositionValidator
    from core.ports.procedure_ports import ProcedureExecutorPort
    from core.ports.runtime_ports import (
        FormatRepairerPort,
        LLMPort,
        RealtimeCallbackPort,
        WorkerToolPort,
    )
    from core.ports.shared_code_context_port import SharedCodeContextPort

logger = logging.getLogger(__name__)

_ASSESSMENT_PARSE_RETRIES = 2

# Recon tools whose full result is a verbatim source file worth persisting into
# the shared code-prefix block. ``read_file`` returns a whole file; ``read_symbol``
# returns only a fragment, so it is intentionally excluded to keep block entries
# at file granularity (matching the worker view-tool capture).
_RECON_SOURCE_READ_TOOLS = frozenset({"read_file"})


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
        capture_recon_reads: bool = False,
        share_boss_recon: bool = False,
        decomposition_validator: "DecompositionValidator | None" = None,
        procedure_executor: "ProcedureExecutorPort | None" = None,
    ) -> None:
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder
        self._child_factory = child_factory
        self._realtime_callback = realtime_callback
        self._llm_query_executor = llm_query_executor or LLMQueryExecutor(llm_port)
        self._toolset_resolver = toolset_resolver or ToolsetPolicyResolver()
        self._format_repairer = format_repairer
        # Deterministic procedure tier. The null default matches nothing, so
        # dispatch is a no-op unless bootstrap binds a domain executor.
        self._procedure_executor = procedure_executor or NullProcedureExecutor()
        self._verification_pipeline = VerificationPipeline(
            llm_port,
            skip_judge=skip_judge,
            format_repairer=format_repairer,
        )
        # Collaborators: malformed-output recovery, decomposition-contract
        # enforcement, and recon/shared-source propagation. Each owns the state
        # and rationale for its concern; the orchestrator only coordinates them.
        self._assessment_recovery = AssessmentRecoveryService(llm_port, format_repairer)
        self._contract = DecompositionContractService(child_factory, decomposition_validator)
        self._recon = ReconPropagationService(
            shared_code_port, capture_recon_reads, share_boss_recon
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
                fixed = self._contract.fixed_decomposition(agent)
                if fixed is not None:
                    fixed_subtasks, selection = fixed
                    agent.record_phase_route(
                        policy_version=selection.policy_version,
                        phase=selection.phase,
                        route=selection.route,
                        evidence_references=list(selection.evidence_references),
                        selected_roles=[subtask.description for subtask in fixed_subtasks],
                        triggers=list(selection.triggers),
                        remaining_budget=selection.remaining_budget,
                    )
                    agent.apply_complexity_result(
                        complexity="complex",
                        reasoning="Domain policy selected a fixed phase-controller route",
                        determined_role=AgentRole.MANAGER,
                    )
                    await self._enforce_and_spawn(agent, fixed_subtasks)
                    return
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

                tool_context = self._toolset_resolver.resolve(agent.role)
                prompt = self._build_assessment_prompt_text(agent)
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
                    recovered_content = (
                        await self._assessment_recovery.recover_assessment_via_reprompt(
                            agent=agent,
                            original_prompt=prompt,
                            narrative=last_response_content,
                            op=op,
                            parse_error=last_parse_error,
                        )
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

    def _build_assessment_prompt_text(self, agent: "AgentSession") -> str:
        """Assemble the task-assessment prompt. Shared by ``assess_task`` and
        ``prime_manager_cache`` so both emit byte-identical text for the same agent."""
        tool_context = self._toolset_resolver.resolve(agent.role)
        return self._prompt_builder.build_assessment_prompt(
            task_description=agent.task_description,
            agent_id=agent.agent_id,
            briefing=agent.briefing,
            hierarchy_limits=agent.hierarchy_limits,
            domain_context=self._get_domain_context(agent),
            scope=self._build_scope(agent),
            prompt_capabilities=build_prompt_capabilities(tool_context),
            shared_code_block=self._resolve_boss_recon_block(agent),
        )

    async def prime_manager_cache(self, agents: "list[AgentSession]") -> int:
        """Pre-warm OpenAI's prompt cache for a batch of depth-1 phase managers.

        Their assessment prompts share a long head (system + persona + operation +
        CVE block) but diverge at per-manager scope, so no manager's request is a
        prefix of another's and none warms the cache for its siblings. This issues
        ONE cheap request carrying their longest common (prompt-prefix, tool defs)
        so each manager's real assessment call reads it back. Returns the primed
        prefix length in chars (0 = skipped: <2 managers or prefix below the floor).
        """
        if len(agents) < 2:
            return 0
        prefix = _longest_common_prefix(
            [self._build_assessment_prompt_text(a) for a in agents]
        )
        if len(prefix) < _MIN_PRIME_PREFIX_CHARS:
            return 0
        anchor = agents[0]
        tool_context = self._toolset_resolver.resolve(anchor.role)
        config = ConfigResolver.resolve(
            anchor.config, operation="task_assessment"
        ).model_dump()
        config["max_tokens"] = _PRIME_MAX_TOKENS
        if anchor.hierarchy_limits is not None:
            config["prompt_cache_key"] = str(anchor.hierarchy_limits.root_id)
        await self._llm_port.query_with_tools(
            messages=[{"role": "user", "content": prefix}],
            config_dict=config,
            tools=list(tool_context.tool_definitions),
        )
        return len(prefix)

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
            fixed = self._contract.fixed_decomposition(agent)
            if fixed is not None:
                fixed_subtasks, selection = fixed
                agent.record_phase_route(
                    policy_version=selection.policy_version,
                    phase=selection.phase,
                    route=selection.route,
                    evidence_references=list(selection.evidence_references),
                    selected_roles=[subtask.description for subtask in fixed_subtasks],
                    triggers=list(selection.triggers),
                    remaining_budget=selection.remaining_budget,
                )
                await self._enforce_and_spawn(agent, fixed_subtasks)
                return
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
                    failure_history=tuple(agent.failure_history),
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
                    shared_code_block=self._resolve_boss_recon_block(agent),
                    failure_history=tuple(agent.failure_history),
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
            subtasks = await self._assessment_recovery.parse_decomposition_with_recovery(
                agent=agent,
                response_content=response.content,
                original_prompt=prompt,
                op=op,
            )
            if subtasks is None:
                # Already failed/marked-infeasible by helper.
                return

            # Enforce the domain role contract before spawning (shared with the
            # assess-time decompose path so the gate cannot be bypassed).
            await self._enforce_and_spawn(agent, subtasks)

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

        # Deterministic tier: registry-matched tasks run host-side with zero
        # LLM turns. A previously failed procedural attempt dispatches agentic
        # (the escalation ladder), never procedural twice.
        procedure_ref = self._resolve_procedure_ref(agent)
        if procedure_ref is not None and not agent.last_attempt_procedural:
            await self._execute_procedure(agent, procedure_ref, persist_checkpoint)
            return

        op = "worker_execution"
        with self._timed_operation(agent, op):
            # Start worker execution (CodeGenerationStarted event, -> IN_PROGRESS)
            tool_name = agent.config.tool
            agent.start_worker_execution(tool_name)

            root_id = agent.hierarchy_limits.root_id if agent.hierarchy_limits else None
            context_packet = await self._recon.resolve_context_packet(
                root_id,
                target_paths=agent.target_paths,
                symbols=agent.symbols,
                failure_digest=agent.failure_digest,
                hard_predecessor_ids=tuple(agent.hard_predecessor_ids),
            )
            shared_code_block = context_packet.source_block if context_packet else None
            shared_code_index = context_packet.source_index if context_packet else None
            if context_packet is not None:
                agent.record_prompt_context(
                    total_chars=context_packet.total_chars,
                    total_tokens=context_packet.total_tokens,
                    segment_sizes={
                        "source": len(context_packet.source_block or ""),
                        "index": len(context_packet.source_index or ""),
                    },
                    entries=[dict(entry) for entry in context_packet.entries],
                    omitted_entries=[
                        dict(entry) for entry in context_packet.omitted_entries
                    ],
                )

            prompt = self._prompt_builder.build_worker_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                handoff=handoff,
                workspace_context=workspace_context,
                domain_context=self._get_domain_context(agent),
                briefing=agent.briefing,
                shared_code_block=shared_code_block,
                shared_code_index=shared_code_index,
            )

            # Inject failure context on retry so the worker knows what to fix
            if agent.retry_count > 0:
                prompt += self._build_retry_context_block(agent)

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
                # Bare task (no prompt wrapping) so adapters can key per-task
                # behavior (e.g. reasoning-effort overrides) off its prefix.
                "task_summary": agent.task_description,
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

    def _resolve_procedure_ref(self, agent: "AgentSession") -> str | None:
        """Dispatch ladder: explicit marking → static registry match.

        Unknown explicit refs downgrade to auto with a warning (fail open —
        a bad LLM marking must never dead-end a task).
        """
        if agent.execution_mode == "agentic":
            return None
        if agent.execution_mode == "procedural":
            if agent.procedure_ref and self._procedure_executor.resolve(agent.procedure_ref):
                return agent.procedure_ref
            logger.warning(
                "Unknown procedure_ref %r on agent %s; downgrading to auto",
                agent.procedure_ref,
                agent.agent_id,
            )
        matched = self._procedure_executor.match(
            agent.task_description, self._get_domain_context(agent)
        )
        if matched is not None and self._procedure_executor.resolve(matched):
            return matched
        return None

    async def _execute_procedure(
        self,
        agent: "AgentSession",
        procedure_ref: str,
        persist_checkpoint: "Callable[[AgentSession], Awaitable[object]] | None",
    ) -> None:
        """Run a registered procedure host-side: no prompt, no LLM, no cost.

        Success completes the worker through the normal verification pipeline;
        failure records the procedure's own digest and fails the worker, which
        the post-step escalation retries agentically.
        """
        op = "procedure_execution"
        with self._timed_operation(agent, op):
            agent.start_procedure(procedure_ref)
            if persist_checkpoint is not None:
                await persist_checkpoint(agent)

            # The run's root_id rides in params so a domain executor can
            # resolve the run's shared container session (core stays
            # topology-only; params remain an opaque dict).
            params: dict[str, Any] = dict(agent.procedure_params)
            if agent.hierarchy_limits is not None:
                params.setdefault("root_id", agent.hierarchy_limits.root_id)

            try:
                result = await self._procedure_executor.execute(
                    procedure_ref,
                    agent.task_description,
                    self._get_domain_context(agent),
                    params,
                )
            except Exception as e:
                # Infrastructure fault (container gone, exec error): fail open
                # into the agentic escalation with the fault as digest.
                logger.warning(
                    "Procedure %s infrastructure fault for agent %s",
                    procedure_ref,
                    agent.agent_id,
                    exc_info=True,
                )
                result = ProcedureResult(
                    success=False,
                    summary=f"Procedure {procedure_ref} infrastructure fault: {e}",
                    digest=f"FAILURE: {e!r}",
                )

            agent.finish_procedure(
                procedure_ref,
                success=result.success,
                summary=result.summary,
                evidence=[item.model_dump() for item in result.evidence],
            )
            if result.success:
                agent.complete_with_result(result.summary)
                await self._verification_pipeline.verify(agent)
            else:
                agent.record_failure_digest(
                    result.digest or result.summary, source="procedure_failure"
                )
                agent.fail_with_reason(result.summary)

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

    async def _enforce_and_spawn(self, agent: "AgentSession", subtasks: list["Subtask"]) -> None:
        """Enforce the decomposition contract, then spawn children.

        Shared by BOTH decomposition paths — ``evaluate_task`` (explicit BOSS/MANAGER
        decomposition) and ``_apply_assessment_result`` (the combined assess+decompose
        single call) — so the role-contract gate cannot be bypassed by either path. The
        agent is failed on contract-unsatisfiable (inside enforcement) or spawn failure.
        """
        # Dedup FIRST so the contract gate has the final word on completeness: a gate that
        # ran before dedup could be undone by dedup dropping a re-injected required role
        # (e.g. on re-decomposition, where the cross-tree role registry still lists the
        # prior attempt's roles). Pass the dedup index shift so authored deps stay valid.
        pre_dedup_index = {id(st): index for index, st in enumerate(subtasks)}
        subtasks = self._dedup_subtasks_against_siblings(agent, subtasks)
        dedup_map = {
            pre_dedup_index[id(st)]: new_index
            for new_index, st in enumerate(subtasks)
            if id(st) in pre_dedup_index
        }
        corrected = await self._enforce_decomposition_contract(agent, subtasks, dedup_map)
        if corrected is None:
            return  # enforcement already failed the agent before any spawn
        failure = await self._spawn_children(agent, corrected)
        if failure:
            agent.fail_with_reason(failure)

    async def _enforce_decomposition_contract(
        self,
        agent: "AgentSession",
        subtasks: list["Subtask"],
        dedup_map: "dict[int, int] | None" = None,
    ) -> "list[Subtask] | None":
        return await self._contract.enforce_decomposition_contract(agent, subtasks, dedup_map)

    def _resolve_catalog_dependencies(
        self, subtasks: list["Subtask"], old_to_new: "dict[int, int] | None" = None
    ) -> list["Subtask"]:
        return self._contract.resolve_catalog_dependencies(subtasks, old_to_new)

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
        # subtasks arrive already deduped, contract-enforced, and dep-resolved from
        # _enforce_and_spawn (dedup runs there, before the gate, so the gate has the final
        # word on completeness). Here: cap-check the final count, then reserve and spawn.
        failure = await self._contract.check_limit_violations(agent, subtasks)
        if failure:
            return failure
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

    def _dedup_subtasks_against_siblings(
        self,
        agent: "AgentSession",
        subtasks: list["Subtask"],
    ) -> list["Subtask"]:
        return self._contract.dedup_subtasks_against_siblings(agent, subtasks)

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

        # Route the assess-time decompose path through the SAME contract gate as
        # evaluate_task — an LLM that assesses+decomposes in one call must not bypass it.
        await self._enforce_and_spawn(agent, result.subtasks)

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
        if os.environ.get("ARISE_PRIME_MANAGER_CACHE") and agent.hierarchy_limits is not None:
            # manager-cache fix (env-gated): shared per-run OpenAI prompt-cache key so the
            # parallel phase managers route to one cache node that prime_manager_cache pre-warms.
            config_dict["prompt_cache_key"] = str(agent.hierarchy_limits.root_id)
        if tool_context is not None and tool_context.has_tools:
            # ALWAYS keep recon read_file verbatim + dedup in the tool loop.
            # This is a manager-side cache optimization (source stays in the
            # cached prefix, no re-reads) with zero worker impact — only
            # boss/managers use this tool-calling path. It is independent of
            # whether reads are also persisted into the shared block (below),
            # which is the worker-affecting half.
            tool_context = replace(
                tool_context, source_read_tools=_RECON_SOURCE_READ_TOOLS
            )
        result = await self._llm_query_executor.query(
            prompt,
            config_dict,
            tool_context=tool_context,
        )
        response = result.response
        self._emit_probe_events(agent, result.tool_records)
        self._capture_recon_source_reads(agent, result.captured_reads)
        self._store_boss_recon_block(agent, result.captured_reads)

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

    @staticmethod
    def _build_retry_context_block(agent: "AgentSession") -> str:
        return build_retry_context_block(agent)

    @staticmethod
    def _get_domain_context(agent: "AgentSession") -> object | None:
        if agent.hierarchy_limits:
            return agent.hierarchy_limits.domain_context
        return None

    async def _resolve_shared_code_block(self, root_id: "UUID | None") -> str | None:
        return await self._recon.resolve_shared_code_block(root_id)

    async def _resolve_shared_code_index(self, root_id: "UUID | None") -> str | None:
        return await self._recon.resolve_shared_code_index(root_id)

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

    def _capture_recon_source_reads(self, agent: "AgentSession", captured_reads: list) -> None:
        self._recon.capture_source_reads(agent, captured_reads)

    def _store_boss_recon_block(self, agent: "AgentSession", captured_reads: list) -> None:
        self._recon.store_boss_recon_block(agent, captured_reads)

    @staticmethod
    def _render_boss_recon_block(captured_reads: list) -> str | None:
        return ReconPropagationService.render_boss_recon_block(captured_reads)

    def _resolve_boss_recon_block(self, agent: "AgentSession") -> str | None:
        return self._recon.resolve_boss_recon_block(agent)
