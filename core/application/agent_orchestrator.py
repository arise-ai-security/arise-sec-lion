"""Agent Orchestrator - coordinates LLM and worker interactions.

This service handles the orchestration logic using composable pipelines,
keeping the domain model pure (no async I/O operations).

The orchestrator delegates to specialized pipelines for:
1. Complexity evaluation (PENDING -> WORKER/MANAGER)
2. Task decomposition (BOSS/MANAGER -> children)
3. Worker execution (WORKER -> complete)

Each pipeline is a sequence of focused, testable steps that together
implement the orchestration logic.
"""



import logging
from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState
from core.application.pipelines import PipelineFactory

if TYPE_CHECKING:
    from core.application.services.child_factory import ChildAgentFactory
    from core.application.services.prompt_builder import PromptBuilder
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.context import SiblingView
    from core.ports.llm_port import LLMPort
    from core.ports.realtime_callback_port import RealtimeCallbackPort
    from core.ports.research_port import ResearchPort
    from core.ports.worker_port import WorkerToolPort

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """Orchestrates agent interactions using composable pipelines.

    This is a thin coordinator that delegates to specialized pipelines for:
    - Complexity evaluation (PENDING -> WORKER/MANAGER)
    - Task decomposition (BOSS/MANAGER -> children)
    - Worker execution (WORKER -> complete)

    Each pipeline is a sequence of focused, testable steps.
    This separation follows SOLID principles:
    - SRP: Each step has one responsibility
    - OCP: New steps can be added without modifying existing ones
    - DIP: Steps depend on abstractions (ports), not implementations
    """

    def __init__(
        self,
        llm_port: "LLMPort",
        worker_port: "WorkerToolPort",
        prompt_builder: "PromptBuilder",
        child_factory: "ChildAgentFactory",
        realtime_callback: "RealtimeCallbackPort | None" = None,
        research_port: "ResearchPort | None" = None,
    ) -> None:
        """Initialize orchestrator with required ports.

        Args:
            llm_port: Port for LLM interactions.
            worker_port: Port for worker tool execution.
            prompt_builder: Builder for constructing prompts.
            child_factory: Factory for child agents (source of truth for agent counts).
            realtime_callback: Optional callback for real-time event streaming.
            research_port: Optional port for RESEARCHER agent tool calling.
        """
        self._pipeline_factory = PipelineFactory(
            llm_port=llm_port,
            worker_port=worker_port,
            prompt_builder=prompt_builder,
            child_factory=child_factory,
            realtime_callback=realtime_callback,
            research_port=research_port,
        )

        # Create pipelines (could also be lazy-created)
        self._complexity_pipeline = self._pipeline_factory.create_complexity_pipeline()
        self._decomposition_pipeline = self._pipeline_factory.create_decomposition_pipeline()
        self._worker_pipeline = self._pipeline_factory.create_worker_pipeline()

        # Research pipeline only if research_port provided
        self._research_pipeline = (
            self._pipeline_factory.create_research_pipeline()
            if research_port
            else None
        )

    async def evaluate_complexity(self, agent: "AgentSession") -> None:
        """Evaluate task complexity for a PENDING agent.

        Performs LLM call and applies result to agent via pure domain methods.
        Automatically detects security tasks and provides security-specific guidance.

        Args:
            agent: The agent to evaluate (must be PENDING with ANALYZING status).
        """
        state = PipelineState(agent=agent, operation="complexity_evaluation")
        result = await self._complexity_pipeline.execute(state)

        if not result.success:
            logger.debug(
                "Complexity evaluation failed for agent=%s: %s",
                agent.agent_id,
                result.failure_reason,
            )
            agent.fail_with_reason(result.failure_reason or "Unknown failure")

    async def evaluate_task(self, agent: "AgentSession") -> None:
        """Decompose task into subtasks for a BOSS/MANAGER agent.

        Performs LLM call, parses subtasks, and spawns children via pure domain methods.
        Respects hierarchy limits (max_depth, max_children_per_node).

        Args:
            agent: The agent to evaluate (must be BOSS/MANAGER with ANALYZING status).
        """
        state = PipelineState(agent=agent, operation="task_decomposition")
        result = await self._decomposition_pipeline.execute(state)

        if not result.success:
            logger.debug(
                "Task decomposition failed for agent=%s: %s",
                agent.agent_id,
                result.failure_reason,
            )
            agent.fail_with_reason(result.failure_reason or "Unknown failure")

    async def execute_task(
        self,
        agent: "AgentSession",
        working_directory: str | None = None,
        workspace_context: str | None = None,
        sibling_view: "SiblingView | None" = None,
    ) -> None:
        """Execute task for a WORKER agent using worker tool.

        Args:
            agent: The agent to execute (must be WORKER with ANALYZING status).
            working_directory: Optional working directory for the worker.
            workspace_context: Optional context about existing workspace files.
            sibling_view: Optional sibling view for coordinated execution.
        """
        state = PipelineState(
            agent=agent,
            operation="worker_execution",
        ).with_worker_context(
            working_directory=working_directory,
            workspace_context=workspace_context,
            sibling_view=sibling_view,
        )

        result = await self._worker_pipeline.execute(state)

        if not result.success:
            logger.debug(
                "Worker execution failed for agent=%s: %s",
                agent.agent_id,
                result.failure_reason,
            )
            agent.fail_with_reason(result.failure_reason or "Unknown failure")

    async def execute_research(
        self,
        agent: "AgentSession",
        working_directory: str | None = None,
    ) -> None:
        """Execute research for a RESEARCHER agent.

        Performs LLM tool calling loop to gather context, then transitions
        the agent to MANAGER role for task decomposition.

        Args:
            agent: The agent to execute (must be RESEARCHER with ANALYZING status).
            working_directory: Working directory for file operations.
        """
        if self._research_pipeline is None:
            agent.fail_with_reason("Research pipeline not configured")
            return

        state = PipelineState(
            agent=agent,
            operation="research_execution",
            working_directory=working_directory,
        )

        result = await self._research_pipeline.execute(state)

        if not result.success:
            logger.debug(
                "Research execution failed for agent=%s: %s",
                agent.agent_id,
                result.failure_reason,
            )
            agent.fail_with_reason(result.failure_reason or "Unknown failure")
