"""Agent Orchestrator - coordinates LLM and worker interactions.

Handles the three orchestration operations as direct method calls:
1. Complexity evaluation (PENDING -> WORKER/MANAGER)
2. Task decomposition (BOSS/MANAGER -> children)
3. Worker execution (WORKER -> complete)
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from core.domain.exceptions import ToolNotAvailableError
from core.domain.services import parse_subtasks_from_llm, strip_markdown_code_block
from core.domain.services.config_resolver import ConfigResolver
from core.domain.values.constraint_failure import ConstraintFailure
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.application.services.child_factory import ChildAgentFactory
    from core.application.services.prompt_builder import PromptBuilder
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.context import SiblingView
    from core.ports.llm_port import LLMPort
    from core.ports.realtime_callback_port import RealtimeCallbackPort
    from core.ports.worker_port import WorkerToolPort

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """Orchestrates agent interactions with LLM and worker tools.

    Three operations:
    - evaluate_complexity: PENDING agent → LLM decides WORKER or MANAGER
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
    ) -> None:
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder
        self._child_factory = child_factory
        self._realtime_callback = realtime_callback

    async def evaluate_complexity(self, agent: "AgentSession") -> None:
        """Evaluate task complexity for a PENDING agent.

        LLM classifies as simple (→ WORKER) or complex (→ MANAGER).
        On failure, agent is failed with reason.
        """
        # Validate
        if agent.role != AgentRole.PENDING:
            agent.fail_with_reason(f"Expected PENDING role, got {agent.role}")
            return

        op = "complexity_evaluation"
        start_time = time.monotonic()
        agent.emit_operation_started(operation_type=op)

        try:
            # Build prompt
            prompt = self._prompt_builder.build_complexity_evaluation_prompt(
                task_description=agent.task_description,
                agent_id=agent.agent_id,
                parent_task=None,
            )
            agent.emit_prompt_sent(prompt=prompt, prompt_type=op, target="llm")

            # Query LLM
            llm_config = ConfigResolver.resolve(agent.config, operation=op)
            response = await self._llm_port.query_with_usage(
                prompt, llm_config.model_dump()
            )
            agent.emit_tokens_consumed(
                model=response.model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cost_usd=response.cost_usd,
                operation=op,
            )

            # Parse complexity result
            clean = strip_markdown_code_block(response.content)
            data = json.loads(clean)
            complexity = data.get("complexity", "").lower()
            reasoning = data.get("reasoning", "")

            if complexity not in ("simple", "complex"):
                agent.fail_with_reason(f"Invalid complexity value: {complexity}")
                return

            # Apply result
            determined_role = (
                AgentRole.WORKER if complexity == "simple" else AgentRole.MANAGER
            )
            agent.apply_complexity_result(
                complexity=complexity,
                reasoning=reasoning,
                determined_role=determined_role,
            )

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            agent.fail_with_reason(f"Failed to parse complexity: {e}")
            return

        finally:
            duration = time.monotonic() - start_time
            agent.emit_operation_finished(operation_type=op, duration_seconds=duration)

    async def evaluate_task(self, agent: "AgentSession") -> None:
        """Decompose task into subtasks for a BOSS/MANAGER agent.

        LLM generates subtasks, limits are enforced, children are spawned.
        On failure, agent is failed with reason.
        """
        if agent.role not in (AgentRole.BOSS, AgentRole.MANAGER):
            agent.fail_with_reason(f"Expected BOSS/MANAGER role, got {agent.role}")
            return

        op = "task_decomposition"
        start_time = time.monotonic()
        agent.emit_operation_started(operation_type=op)

        try:
            # Build role-specific prompt
            if agent.role == AgentRole.BOSS:
                prompt = self._prompt_builder.build_boss_delegation_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    cve_instance=self._get_cve_instance(agent),
                    hierarchy_limits=agent.hierarchy_limits,
                )
            else:
                prompt = self._prompt_builder.build_manager_decomposition_prompt(
                    task_description=agent.task_description,
                    agent_id=agent.agent_id,
                    cve_instance=self._get_cve_instance(agent),
                    spawn_payload=agent.spawn_payload,
                    hierarchy_limits=agent.hierarchy_limits,
                )
            agent.emit_prompt_sent(prompt=prompt, prompt_type=op, target="llm")

            # Query LLM
            llm_config = ConfigResolver.resolve(agent.config, operation=op)
            response = await self._llm_port.query_with_usage(
                prompt, llm_config.model_dump()
            )
            agent.emit_tokens_consumed(
                model=response.model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cost_usd=response.cost_usd,
                operation=op,
            )

            # Parse subtasks
            try:
                result = parse_subtasks_from_llm(response.content)
            except ValueError as e:
                logger.warning(
                    "Failed to parse subtasks for agent=%s: %s", agent.agent_id, e
                )
                agent.fail_with_reason(f"Failed to parse subtasks: {e}")
                return

            if isinstance(result, ConstraintFailure):
                logger.warning(
                    "Agent %s reported unsatisfiable constraints: %s",
                    agent.agent_id,
                    result.format_message(),
                )
                agent.fail_with_reason(result.format_message())
                return

            subtasks = result

            # Check hard limits
            failure = self._check_limit_violations(agent, subtasks)
            if failure:
                agent.fail_with_reason(failure)
                return

            # Determine child role (soft depth limit)
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

            child_role = (
                AgentRole.WORKER.value if force_worker else AgentRole.PENDING.value
            )

            # Spawn children
            spawn_payload = agent.get_spawn_payload_for_child()
            agent.apply_subtasks_and_spawn_children(
                subtasks=subtasks,
                child_role=child_role,
                spawn_payload=spawn_payload,
            )

        finally:
            duration = time.monotonic() - start_time
            agent.emit_operation_finished(operation_type=op, duration_seconds=duration)

    async def execute_task(
        self,
        agent: "AgentSession",
        working_directory: str | None = None,
        workspace_context: str | None = None,
        sibling_view: "SiblingView | None" = None,
    ) -> None:
        """Execute task for a WORKER agent using worker tool.

        On failure, agent is failed with reason.
        """
        if agent.role != AgentRole.WORKER:
            agent.fail_with_reason(f"Expected WORKER role, got {agent.role}")
            return

        op = "worker_execution"
        start_time = time.monotonic()
        agent.emit_operation_started(operation_type=op)

        try:
            # Start worker execution (CodeGenerationStarted event, -> IN_PROGRESS)
            tool_name = agent.config.tool
            agent.start_worker_execution(tool_name)

            # Build worker prompt
            prompt = self._prompt_builder.build_worker_prompt(
                task_description=agent.task_description,
                sibling_view=sibling_view,
                workspace_context=workspace_context,
                cve_instance=self._get_cve_instance(agent),
                spawn_payload=agent.spawn_payload,
            )
            agent.emit_prompt_sent(
                prompt=prompt, prompt_type=op, target=tool_name
            )

            # Run worker session
            task_context: dict[str, Any] = {
                "agent_id": agent.agent_id,
                "task_description": prompt,
                "tool_name": tool_name,
                "config": agent.config,
            }
            if working_directory:
                task_context["working_directory"] = working_directory

            root_id = (
                agent.hierarchy_limits.root_id
                if agent.hierarchy_limits
                else None
            )

            try:
                async for tool_event in self._worker_port.run_session(task_context):
                    agent.apply_worker_event(tool_event)
                    if self._realtime_callback and root_id:
                        await self._realtime_callback.on_event(tool_event, root_id)
            except ToolNotAvailableError as e:
                agent.fail_with_reason(str(e))
                return

        finally:
            duration = time.monotonic() - start_time
            agent.emit_operation_finished(operation_type=op, duration_seconds=duration)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _check_limit_violations(
        self, agent: "AgentSession", subtasks: list
    ) -> str | None:
        """Check hard limits. Returns failure reason or None."""
        limits = agent.hierarchy_limits
        violations: list[dict[str, Any]] = []

        # Hard limit: children per node
        if limits is not None and limits.is_children_limited():
            if len(subtasks) > limits.max_children_per_node:
                violations.append({
                    "limit_type": "children",
                    "limit_value": limits.max_children_per_node,
                    "attempted_value": len(subtasks),
                    "action_taken": "agent_failed",
                })

        # Hard limit: total agents
        max_total = self._child_factory.max_total_agents
        if max_total > 0:
            current_total = self._child_factory.total_created
            remaining = max_total - current_total
            if len(subtasks) > remaining:
                violations.append({
                    "limit_type": "total_agents",
                    "limit_value": max_total,
                    "attempted_value": current_total + len(subtasks),
                    "action_taken": "agent_failed",
                })

        if violations:
            for v in violations:
                agent.emit_limit_enforced(**v)
            types = [v["limit_type"] for v in violations]
            return f"LLM violated limits: {types}"

        return None

    @staticmethod
    def _get_cve_instance(agent: "AgentSession"):
        """Extract CVE instance from hierarchy limits if available."""
        if agent.hierarchy_limits:
            return agent.hierarchy_limits.cve_instance
        return None
