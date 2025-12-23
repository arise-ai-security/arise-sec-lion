"""Agent Orchestrator - coordinates LLM and worker interactions.

This service handles the orchestration logic that was previously in AgentSession,
keeping the domain model pure (no async I/O operations).

The orchestrator:
1. Performs LLM calls for complexity evaluation and task decomposition
2. Executes worker tasks
3. Calls pure domain methods on AgentSession to emit events
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from core.domain.config_resolver import ConfigResolver
from core.domain.exceptions import ToolNotAvailableError
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.prompt_builder import PromptBuilder, is_security_task
from core.domain.services import SubtaskParser, strip_markdown_code_block

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.ports.llm_port import LLMPort
    from core.ports.worker_port import WorkerToolPort


class AgentOrchestrator:
    """Orchestrates agent interactions with LLM and worker tools.

    This separates infrastructure concerns (LLM calls, worker execution)
    from the domain model, following DDD principles.
    """

    def __init__(
        self,
        llm_port: LLMPort,
        worker_port: WorkerToolPort,
        prompt_builder: PromptBuilder,
    ) -> None:
        """Initialize orchestrator with required ports.

        Args:
            llm_port: Port for LLM interactions.
            worker_port: Port for worker tool execution.
            prompt_builder: Builder for constructing prompts.
        """
        self._llm_port = llm_port
        self._worker_port = worker_port
        self._prompt_builder = prompt_builder

    async def evaluate_complexity(self, agent: AgentSession) -> None:
        """Evaluate task complexity for a PENDING agent.

        Performs LLM call and applies result to agent via pure domain methods.
        Automatically detects security tasks and provides security-specific guidance.

        Args:
            agent: The agent to evaluate (must be PENDING with ANALYZING status).
        """
        assert agent.role == AgentRole.PENDING, f"Requires PENDING role, got {agent.role}"
        assert agent.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {agent.status}"
        assert agent.task_description, "Requires assigned task"

        # Build complexity evaluation prompt
        prompt = self._prompt_builder.build_complexity_evaluation_prompt(
            task_description=agent.task_description,
            agent_id=agent.session_id,
            parent_task=None,
        )

        # Append security-specific guidance for security tasks
        if is_security_task(agent.task_description):
            try:
                security_guidance = self._prompt_builder.env.get_template(
                    "security/complexity_security.j2"
                ).render()
                prompt = f"{prompt}\n\n{security_guidance}"
            except Exception:
                logger.debug("Security template not found, using base prompt")

        # Perform LLM call
        llm_config = ConfigResolver.resolve(agent.config, operation="complexity_evaluation")
        llm_response = await self._llm_port.query_with_usage(prompt, llm_config.model_dump())

        # Emit cost tracking event via pure domain method
        agent.emit_tokens_consumed(
            model=llm_response.model,
            prompt_tokens=llm_response.usage.prompt_tokens,
            completion_tokens=llm_response.usage.completion_tokens,
            total_tokens=llm_response.usage.total_tokens,
            cost_usd=llm_response.cost_usd,
            operation="complexity_evaluation",
        )

        # Parse response
        try:
            clean_response = strip_markdown_code_block(llm_response.content)
            data = json.loads(clean_response)
            complexity = data.get("complexity", "").lower()
            reasoning = data.get("reasoning", "")

            if complexity not in ("simple", "complex"):
                raise ValueError(f"Invalid complexity value: {complexity}")

            determined_role = AgentRole.WORKER if complexity == "simple" else AgentRole.MANAGER

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            agent.fail_with_reason(f"Failed to evaluate complexity: {e}")
            return

        # Apply result via pure domain method
        agent.apply_complexity_result(
            complexity=complexity,
            reasoning=reasoning,
            determined_role=determined_role,
        )

    async def evaluate_task(self, agent: AgentSession) -> None:
        """Decompose task into subtasks for a BOSS/MANAGER agent.

        Performs LLM call, parses subtasks, and spawns children via pure domain methods.
        Respects execution context limits (max_depth, max_children_per_node).

        Args:
            agent: The agent to evaluate (must be BOSS/MANAGER with ANALYZING status).
        """
        assert agent.role in (AgentRole.BOSS, AgentRole.MANAGER), (
            f"Requires BOSS/MANAGER, got {agent.role}"
        )
        assert agent.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {agent.status}"

        # Build prompt using auto-detection for security tasks
        prompt = self._prompt_builder.build_auto_prompt(
            task_description=agent.task_description,
            agent_id=agent.session_id,
            agent_role=agent.role.value.upper(),
            parent_task=None,
        )

        # Perform LLM call
        llm_config = ConfigResolver.resolve(agent.config, operation="task_decomposition")
        llm_response = await self._llm_port.query_with_usage(prompt, llm_config.model_dump())

        # Emit cost tracking event
        agent.emit_tokens_consumed(
            model=llm_response.model,
            prompt_tokens=llm_response.usage.prompt_tokens,
            completion_tokens=llm_response.usage.completion_tokens,
            total_tokens=llm_response.usage.total_tokens,
            cost_usd=llm_response.cost_usd,
            operation="task_decomposition",
        )

        # Parse subtasks
        try:
            subtasks = SubtaskParser.parse_from_llm_response(llm_response.content)
        except ValueError as e:
            agent.fail_with_reason(f"Failed to parse subtasks: {e}")
            return

        # Enforce max_children_per_node limit
        if agent.execution_context is not None and agent.execution_context.is_children_limited():
            max_children = agent.execution_context.max_children_per_node
            if len(subtasks) > max_children:
                agent.emit_limit_enforced(
                    limit_type="children",
                    limit_value=max_children,
                    attempted_value=len(subtasks),
                    action_taken="truncated_subtasks",
                )
                subtasks = subtasks[:max_children]

        # Determine child role based on depth limit
        force_worker = False
        if agent.execution_context is not None and not agent.execution_context.can_spawn_child():
            force_worker = True
            agent.emit_limit_enforced(
                limit_type="depth",
                limit_value=agent.execution_context.max_depth,
                attempted_value=agent.execution_context.current_depth + 1,
                action_taken="forced_worker_role",
            )

        child_role = AgentRole.WORKER.value if force_worker else AgentRole.PENDING.value

        # Build parent context and spawn children via pure domain method
        parent_context = agent.get_context_for_child()
        agent.apply_subtasks_and_spawn_children(
            subtasks=subtasks,
            child_role=child_role,
            parent_context=parent_context,
        )

    async def execute_task(
        self,
        agent: AgentSession,
        working_directory: str | None = None,
        workspace_context: str | None = None,
    ) -> None:
        """Execute task for a WORKER agent using worker tool.

        Args:
            agent: The agent to execute (must be WORKER with ANALYZING status).
            working_directory: Optional working directory for the worker.
            workspace_context: Optional context about existing workspace files.
        """
        assert agent.role == AgentRole.WORKER, f"Requires WORKER, got {agent.role}"
        assert agent.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {agent.status}"

        tool_name = agent.config.tool

        # Emit start event via pure domain method
        agent.start_worker_execution(tool_name)

        # Build enhanced task description using template
        worker_instructions = self._prompt_builder.env.get_template(
            "worker/execution_instructions.j2"
        ).render()
        enhanced_description = f"{worker_instructions}\n\n<TASK>\n{agent.task_description}\n</TASK>"

        if workspace_context:
            enhanced_description += (
                f"\n\n<WORKSPACE_CONTEXT>\n"
                f"You are working in a shared workspace. Other workers may have created files.\n"
                f"Current files in workspace:\n{workspace_context}\n"
                f"</WORKSPACE_CONTEXT>"
            )

        task_context: dict[str, Any] = {
            "session_id": agent.session_id,
            "task_description": enhanced_description,
            "tool_name": tool_name,
            "config": agent.config,
        }

        if working_directory:
            task_context["working_directory"] = working_directory

        # Execute via worker port, applying events through pure domain method
        try:
            async for tool_event in self._worker_port.run_session(task_context):
                agent.apply_worker_event(tool_event)
        except ToolNotAvailableError as e:
            agent.fail_with_reason(str(e))
