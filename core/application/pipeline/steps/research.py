"""Research pipeline steps for RESEARCHER agent execution.

These steps implement the research phase where RESEARCHER agents gather
context via LLM tool calling before transitioning to MANAGER role.
"""

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole, AgentStatus
from core.ports.research_port import DEFAULT_RESEARCH_TOOLS, ToolCallProgress

if TYPE_CHECKING:
    from core.application.services.prompt_builder import PromptBuilder
    from core.ports.research_port import ResearchPort


class BuildResearchPrompt:
    """Build system prompt for RESEARCHER using PromptBuilder.

    This step uses the same prompt building pattern as other agents,
    ensuring RESEARCHER gets SEC-bench context (CVE instance, constraints).
    """

    def __init__(self, prompt_builder: "PromptBuilder") -> None:
        """Initialize with prompt builder.

        Args:
            prompt_builder: Builder for constructing domain-aware prompts
        """
        self._prompt_builder = prompt_builder

    async def execute(self, state: PipelineState) -> StepResult:
        """Build researcher prompt and store in state."""
        agent = state.agent

        prompt = self._prompt_builder.build_researcher_prompt(
            task_description=agent.task_description,
            cve_instance=state.cve_instance,
            spawn_payload=agent.spawn_payload,
        )

        return StepResult.ok(state.with_prompt(prompt))


class StartResearch:
    """Emit ResearchStarted event and transition agent to IN_PROGRESS.

    This step marks the beginning of the research phase.
    """

    def __init__(self, available_tools: list[str] | None = None) -> None:
        """Initialize with available tools.

        Args:
            available_tools: Tools to make available (default: file_read, grep_search, list_files)
        """
        self._tools = available_tools or DEFAULT_RESEARCH_TOOLS

    async def execute(self, state: PipelineState) -> StepResult:
        """Start research phase."""
        agent = state.agent

        # Validate agent is RESEARCHER in ANALYZING state
        if agent.role != AgentRole.RESEARCHER:
            return StepResult.fail(f"Expected RESEARCHER role, got {agent.role}")
        if agent.status != AgentStatus.ANALYZING:
            return StepResult.fail(f"Expected ANALYZING status, got {agent.status}")

        # Emit ResearchStarted event
        agent.start_research(self._tools)

        return StepResult.ok(state)


class RunResearchSession:
    """Execute LLM tool calling loop via ResearchPort.

    This step runs the actual research by calling the ResearchPort,
    which handles the tool calling loop with the LLM.
    Emits ResearchToolCalled events after each tool execution for real-time progress.
    """

    def __init__(
        self,
        research_port: "ResearchPort",
        available_tools: list[str] | None = None,
        max_tool_calls: int = 10,
    ) -> None:
        """Initialize with research port.

        Args:
            research_port: Port for executing research
            available_tools: Tools to enable (default: all read-only tools)
            max_tool_calls: Maximum tool calls before forcing summary
        """
        self._research_port = research_port
        self._tools = available_tools or DEFAULT_RESEARCH_TOOLS
        self._max_tool_calls = max_tool_calls

    async def execute(self, state: PipelineState) -> StepResult:
        """Run research session and store results."""
        agent = state.agent

        # Task description is always from agent (what to research)
        task = agent.task_description

        # System prompt is built by BuildResearchPrompt step (stored in state.prompt)
        system_prompt = state.prompt

        # Get working directory from state or use default
        working_directory = state.working_directory or "."

        # Create callback that emits ResearchToolCalled events on the agent
        def on_tool_call(progress: ToolCallProgress) -> None:
            """Emit ResearchToolCalled event for each tool execution."""
            agent.emit_research_tool_called(
                tool_name=progress.tool_name,
                tool_arguments=progress.arguments,
                result_preview=progress.result_preview,
                iteration=progress.iteration,
            )

        # Run research session with progress callback and system prompt
        result = await self._research_port.run_research_session(
            task_description=task,
            working_directory=working_directory,
            config_dict=agent.config.base.model_dump(),
            available_tools=self._tools,
            max_tool_calls=self._max_tool_calls,
            on_tool_call=on_tool_call,
            system_prompt=system_prompt,
        )

        # Update state with research results
        return StepResult.ok(
            state.with_research_result(
                findings=result.findings,
                context={
                    "tool_calls": result.tool_calls,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
        )


class ApplyResearchResult:
    """Apply research results and transition to MANAGER role.

    This step:
    1. Emits ResearchCompleted event with findings
    2. Emits RoleTransitioned event (RESEARCHER -> MANAGER)
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Apply research result and transition role."""
        agent = state.agent

        if state.research_findings is None:
            return StepResult.fail("No research findings in state")

        context = state.research_context or {}

        # Emit ResearchCompleted event
        agent.apply_research_result(
            findings=state.research_findings,
            tool_calls_count=len(context.get("tool_calls", [])),
            gathered_context=context,
        )

        # Transition to MANAGER role
        agent.transition_to_manager(reason="Research phase completed")

        return StepResult.ok(state)


class EmitResearchTokensConsumed:
    """Emit TokensConsumed event for research phase.

    Tracks token usage from the research tool calling loop.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Emit token consumption event for research."""
        agent = state.agent
        context = state.research_context or {}

        prompt_tokens = context.get("prompt_tokens", 0)
        completion_tokens = context.get("completion_tokens", 0)
        total_tokens = prompt_tokens + completion_tokens

        if total_tokens > 0:
            # Get model from agent config
            model = agent.config.base.model

            # Calculate cost (simple estimate)
            # TODO: Use proper cost calculator
            cost_usd = 0.0

            agent.emit_tokens_consumed(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
                operation="research_execution",
            )

        return StepResult.ok(state)
