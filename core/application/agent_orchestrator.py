"""Agent Orchestrator - coordinates LLM and worker interactions.

Handles the three orchestration operations as direct method calls:
1. Task assessment (PENDING -> WORKER or MANAGER+children in single call)
2. Task decomposition (BOSS/MANAGER -> children)
3. Worker execution (WORKER -> complete)
"""

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from core.application.services import SubtaskScope, build_prompt_capabilities
from core.domain.exceptions import InfeasibleError, ToolNotAvailableError
from core.domain.services import (
    AssessmentResult,
    parse_assessment_response,
    parse_subtasks_from_llm,
)
from core.domain.services.config_resolver import ConfigResolver
from core.domain.values.enums import AgentRole, AgentStatus


from core.application.services import (
    LLMQueryExecutor,
    ToolsetPolicyResolver,
    VerificationPipeline,
)

if TYPE_CHECKING:
    from core.application.services import (
        ChildAgentFactory,
        PromptBuilder,
    )
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.llm_response import LLMResponse
    from core.domain.values.node_message import Handoff
    from core.domain.values.subtask import Subtask
    from core.ports.runtime_ports import (
        LLMPort,
        RealtimeCallbackPort,
        WorkerToolPort,
    )

logger = logging.getLogger(__name__)

_JSON_PARSE_RETRIES = 3

# Structured prefix used on agent fail_with_reason messages when the LLM's
# JSON output could not be parsed after all retries. The experiment-level
# anomaly detector greps for this prefix to distinguish infrastructure
# failures from business-level outcomes.
MALFORMED_JSON_REASON_PREFIX = "malformed_json_after_retries"


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
    ) -> None:
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder
        self._child_factory = child_factory
        self._realtime_callback = realtime_callback
        self._llm_query_executor = llm_query_executor or LLMQueryExecutor(llm_port)
        self._toolset_resolver = toolset_resolver or ToolsetPolicyResolver()
        self._verification_pipeline = VerificationPipeline(llm_port, skip_judge=skip_judge)

    async def assess_task(self, agent: "AgentSession") -> None:
        """Assess a PENDING agent: execute directly or decompose.

        Single LLM call replaces separate complexity evaluation + decomposition.
        On execute → WORKER. On decompose → MANAGER with children spawned.
        """
        if agent.role != AgentRole.PENDING:
            agent.fail_with_reason(f"Expected PENDING role, got {agent.role}")
            return

        op = "task_assessment"
        with self._timed_operation(agent, op):
            try:
                domain_context = self._get_domain_context(agent)
                tool_context = self._toolset_resolver.resolve(agent.role)

                # Build prompt
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
                agent.emit_prompt_sent(prompt=prompt, prompt_type=op, target="llm")

                # Query LLM and parse; retry on malformed JSON
                last_parse_error: Exception | None = None
                for attempt in range(_JSON_PARSE_RETRIES):
                    response = await self._query_llm(
                        agent,
                        prompt,
                        op,
                        tool_context=tool_context,
                    )
                    try:
                        result = parse_assessment_response(response.content)
                        break
                    except (json.JSONDecodeError, ValueError, KeyError) as e:
                        last_parse_error = e
                        if attempt < _JSON_PARSE_RETRIES - 1:
                            logger.warning(
                                "Assessment parse attempt %d/%d failed for "
                                "agent=%s: %s",
                                attempt + 1,
                                _JSON_PARSE_RETRIES,
                                agent.agent_id,
                                e,
                            )
                else:
                    agent.fail_with_reason(
                        f"{MALFORMED_JSON_REASON_PREFIX}: Assessment failed after "
                        f"{_JSON_PARSE_RETRIES} attempts: {last_parse_error}"
                    )
                    return

                self._apply_assessment_result(agent, result)
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                agent.fail_with_reason(f"Assessment failed: {e}")

    async def evaluate_task(self, agent: "AgentSession") -> None:
        """Decompose task into subtasks for a BOSS/MANAGER agent.

        LLM generates subtasks, limits are enforced, children are spawned.
        On failure, agent is failed with reason.
        """
        if agent.role not in (AgentRole.BOSS, AgentRole.MANAGER):
            agent.fail_with_reason(f"Expected BOSS/MANAGER role, got {agent.role}")
            return

        op = "task_decomposition"
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
            agent.emit_prompt_sent(prompt=prompt, prompt_type=op, target="llm")

            # Query LLM and parse; retry on malformed JSON (mirrors assess_task).
            # InfeasibleError is a valid outcome, not a retryable parse failure.
            last_parse_error: Exception | None = None
            subtasks = None
            for attempt in range(_JSON_PARSE_RETRIES):
                response = await self._query_llm(
                    agent,
                    prompt,
                    op,
                    tool_context=tool_context,
                )
                try:
                    subtasks = parse_subtasks_from_llm(response.content)
                    break
                except InfeasibleError as e:
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
                    return
                except ValueError as e:
                    last_parse_error = e
                    if attempt < _JSON_PARSE_RETRIES - 1:
                        logger.warning(
                            "Decomposition parse attempt %d/%d failed for agent=%s: %s",
                            attempt + 1,
                            _JSON_PARSE_RETRIES,
                            agent.agent_id,
                            e,
                        )

            if subtasks is None:
                agent.fail_with_reason(
                    f"{MALFORMED_JSON_REASON_PREFIX}: Decomposition failed after "
                    f"{_JSON_PARSE_RETRIES} attempts: {last_parse_error}"
                )
                return

            failure = self._spawn_children(agent, subtasks)
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
    ) -> None:
        """Execute task for a WORKER agent using worker tool.

        On failure, agent is failed with reason.
        """
        if agent.role != AgentRole.WORKER:
            agent.fail_with_reason(f"Expected WORKER role, got {agent.role}")
            return

        op = "worker_execution"
        with self._timed_operation(agent, op):
            # Start worker execution (CodeGenerationStarted event, -> IN_PROGRESS)
            tool_name = agent.config.tool
            agent.start_worker_execution(tool_name)

            # Build worker prompt
            prompt = self._prompt_builder.build_worker_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                handoff=handoff,
                workspace_context=workspace_context,
                domain_context=self._get_domain_context(agent),
                briefing=agent.briefing,
            )

            # Inject verification feedback on retry so worker knows what to fix
            if (
                agent.failed_by_verification
                and agent.verification_feedback
                and (agent.retry_count > 0 or agent.verification_retry_count > 0)
            ):
                if agent.success_criteria:
                    prompt += (
                        f"\n\n**Success criteria you MUST satisfy:**\n"
                        f"{agent.success_criteria}\n"
                    )
            prompt = self._append_previous_attempt_feedback(prompt, agent)

            agent.emit_prompt_sent(prompt=prompt, prompt_type=op, target=tool_name)

            # Run worker session
            task_context: dict[str, Any] = {
                "agent_id": agent.agent_id,
                "task_description": prompt,
                "tool_name": tool_name,
                "config": agent.config,
            }
            if working_directory:
                task_context["working_directory"] = working_directory
            if task_context_overrides:
                task_context.update(task_context_overrides)

            root_id = agent.hierarchy_limits.root_id if agent.hierarchy_limits else None

            try:
                async for tool_event in self._worker_port.run_session(task_context):
                    agent.apply_worker_event(tool_event)
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

    def _check_limit_violations(self, agent: "AgentSession", subtasks: list) -> str | None:
        """Check hard limits. Returns failure reason or None."""
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

    def _spawn_children(
            self,
            agent: "AgentSession",
            subtasks: list["Subtask"],
    ) -> str | None:
        """Validate limits, determine child role, and spawn children."""
        failure = self._check_limit_violations(agent, subtasks)
        if failure:
            return failure

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

    def _apply_assessment_result(
            self,
            agent: "AgentSession",
            result: AssessmentResult,
    ) -> None:
        """Apply the parsed assessment outcome to the agent."""
        if result.action == "infeasible":
            if result.constraint_failure is None:
                agent.fail_with_reason(
                    "Assessment reported infeasible but no failure details"
                )
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

        failure_msg = self._spawn_children(agent, result.subtasks)
        if failure_msg:
            agent.fail_with_reason(failure_msg)

    async def _query_llm(
            self,
            agent: "AgentSession",
            prompt: str,
            operation: str,
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
            agent=agent,
        )
        response = result.response
        self._emit_probe_events(agent, result.tool_records)

        agent.emit_tokens_consumed(
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cost_usd=response.cost_usd,
            operation=operation,
        )

        return response

    @staticmethod
    def _get_domain_context(agent: "AgentSession") -> object | None:
        """Extract optional domain context from hierarchy limits."""
        if agent.hierarchy_limits:
            return agent.hierarchy_limits.domain_context
        return None

    @staticmethod
    def _build_scope(agent: "AgentSession") -> SubtaskScope | None:
        """Build SubtaskScope from agent's structured scoping fields."""
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
        """Emit ProbeStarted/ProbeCompleted events for each recon tool call."""
        for record in tool_records:
            agent.emit_probe_started(probe_type=record.tool_name)
            agent.emit_probe_completed(
                probe_type=record.tool_name,
                result_summary=record.result_summary,
            )

    @staticmethod
    def _append_previous_attempt_feedback(
            prompt: str,
            agent: "AgentSession",
    ) -> str:
        """Append prior failure context so retries can self-correct."""
        if (
            (agent.retry_count <= 0 and agent.verification_retry_count <= 0)
            or not agent.last_retry_reason
        ):
            return prompt

        feedback = agent.last_retry_reason.strip()
        if not feedback:
            return prompt

        return (
            f"{prompt}\n\n"
            "<previous_attempt_feedback>\n"
            f"{feedback}\n"
            "</previous_attempt_feedback>"
        )
