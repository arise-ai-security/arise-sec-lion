"""Domain service for building hierarchical prompts from Jinja2 templates."""



from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.values.enums import AgentRole
from core.application.services.prompt_strategy import DefaultPromptStrategy, PromptContext, PromptStrategy

if TYPE_CHECKING:
    from core.domain.values.context import ParentContext
    from core.domain.values.cve_instance import CVEInstance
    from core.domain.values.execution_context import ExecutionContext
    from core.domain.services import RegisteredTask


class TemplateChain:
    """Fluent builder for chaining template renders."""

    __slots__ = ("_env", "_parts", "_error_context")

    def __init__(
        self,
        env: Environment,
        error_context: str = "template",
    ) -> None:
        self._env = env
        self._parts: list[str] = []
        self._error_context = error_context

    def _template_exists(self, name: str) -> bool:
        """Check if a template exists without raising."""
        try:
            self._env.get_template(name)
            return True
        except TemplateNotFound:
            return False

    def render(self, template: str, **kwargs: Any) -> Self:
        """Render a template and add to chain."""
        try:
            self._parts.append(self._env.get_template(template).render(**kwargs))
        except TemplateNotFound as e:
            raise TemplateNotFound(f"Required {self._error_context} not found: {e.name}") from e
        return self

    def render_if(self, condition: Any, template: str, **kwargs: Any) -> Self:
        """Render a template only if condition is truthy."""
        if condition:
            return self.render(template, **kwargs)
        return self

    def render_optional(self, template: str, **kwargs: Any) -> Self:
        """Render a template if it exists, skip silently otherwise."""
        if self._template_exists(template):
            self._parts.append(self._env.get_template(template).render(**kwargs))
        return self

    def with_security(self, overlay_name: str, **kwargs: Any) -> Self:
        """Inject security template if it exists. Looks in security/ directory."""
        return self.render_optional(f"security/{overlay_name}", **kwargs)

    def with_secbench(self, template_name: str, **kwargs: Any) -> Self:
        """Inject SEC-bench template if it exists. Looks in secbench/ directory."""
        return self.render_optional(f"secbench/{template_name}", **kwargs)

    def with_cve_context(self, cve_instance: "CVEInstance | None") -> Self:
        """Inject SEC-bench CVE context if available.

        Renders secbench/context.j2 with CVE instance data.
        """
        if cve_instance is not None:
            return self.render_optional(
                "secbench/context.j2",
                **cve_instance.to_template_context(),
            )
        return self

    def text(self, content: str) -> Self:
        """Add raw text to chain."""
        self._parts.append(content)
        return self

    def text_if(self, condition: Any, content: str) -> Self:
        """Add raw text only if condition is truthy."""
        if condition:
            self._parts.append(content)
        return self

    def build(self, separator: str = "\n\n") -> str:
        """Join all parts into final prompt."""
        return separator.join(self._parts)


class PromptBuilder:
    """Compose hierarchical prompts with automatic security injection.

    Uses Strategy Pattern to delegate specialized prompt building
    (e.g., SEC-bench) to injected strategies, following SRP.
    """

    def __init__(
        self,
        template_dir: str | Path,
        default_tool: str,
        strategy: PromptStrategy | None = None,
    ) -> None:
        """Initialize prompt builder.

        Args:
            template_dir: Directory containing Jinja2 templates.
            default_tool: Default tool name for worker execution.
            strategy: Optional prompt strategy for specialized prompts.
        """
        self.template_dir = Path(template_dir)
        self.default_tool = default_tool
        self._strategy = strategy or DefaultPromptStrategy()
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,  # noqa: S701 - plain text prompts, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def chain(self) -> TemplateChain:
        """Create a new template chain builder."""
        return TemplateChain(self.env, "template")

    def set_strategy(self, strategy: PromptStrategy) -> None:
        """Set the prompt strategy after initialization.

        Useful for breaking circular dependencies when the strategy
        needs access to this builder's chain() method.
        """
        self._strategy = strategy

    def _task_context(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole,
        parent_task: str | None = None,
    ) -> dict[str, Any]:
        """Build common task context dict."""
        return {
            "task_description": task_description,
            "agent_id": str(agent_id),
            "agent_role": agent_role.value.upper(),
            "parent_task": parent_task,
            "default_tool": self.default_tool,
        }

    def _limits_context(
        self,
        execution_context: "ExecutionContext | None",
    ) -> dict[str, Any]:
        """Build limits context dict for templates.

        Args:
            execution_context: Current execution context with limits.

        Returns:
            Dict with limit variables for templates:
            - max_subtasks: Max children allowed (None = no limit)
            - depth_remaining: Levels of hierarchy remaining (None = unlimited)
            - at_max_depth: True if children must be workers
            - agents_remaining: How many more agents can be created (None = unlimited)
            - max_total_agents: Maximum total agents allowed (None = unlimited)
            - current_total_agents: Current count of agents created (None = unknown)
        """
        if execution_context is None:
            return {
                "max_subtasks": None,
                "depth_remaining": None,
                "at_max_depth": False,
                "agents_remaining": None,
                "max_total_agents": None,
                "current_total_agents": None,
            }

        max_subtasks = None
        if execution_context.is_children_limited():
            max_subtasks = execution_context.max_children_per_node

        depth_remaining = None
        at_max_depth = False
        if execution_context.is_depth_limited():
            depth_remaining = execution_context.max_depth - execution_context.current_depth
            at_max_depth = not execution_context.can_spawn_child()

        # Total agents limit info
        agents_remaining = None
        max_total_agents = None
        current_total_agents = None
        if execution_context.is_total_agents_limited():
            agents_remaining = execution_context.agents_remaining()
            max_total_agents = execution_context.max_total_agents
            current_total_agents = execution_context.current_total_agents

        return {
            "max_subtasks": max_subtasks,
            "depth_remaining": depth_remaining,
            "at_max_depth": at_max_depth,
            "agents_remaining": agents_remaining,
            "max_total_agents": max_total_agents,
            "current_total_agents": current_total_agents,
        }

    def build_complexity_evaluation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
    ) -> str:
        """Build prompt for PENDING agent complexity evaluation."""
        return (
            self.chain()
            .render("core/roles/pending.j2")
            .render("core/strategies/complexity.j2")
            .text(f"<TASK_TO_EVALUATE>\n{task_description}\n</TASK_TO_EVALUATE>")
            .text_if(parent_task, f"<PARENT_TASK>\n{parent_task}\n</PARENT_TASK>")
            .render("core/output/complexity.j2")
            .with_security("complexity_overlay.j2")
            .build()
        )

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole = AgentRole.MANAGER,
        parent_task: str | None = None,
        registered_tasks: "list[RegisteredTask] | None" = None,
        cve_instance: "CVEInstance | None" = None,
        parent_context: "ParentContext | None" = None,
        execution_context: "ExecutionContext | None" = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition.

        Args:
            task_description: The task to decompose.
            agent_id: Current agent's ID.
            agent_role: Agent role (should be MANAGER).
            parent_task: Parent task description.
            registered_tasks: List of already-registered tasks (for dedup hint).
            cve_instance: CVE instance for benchmark runs.
            parent_context: Parent context for hierarchy info.
            execution_context: Execution context with limits.
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=agent_role,
            default_tool=self.default_tool,
            parent_task=parent_task,
            parent_context=parent_context,
            execution_context=execution_context,
            cve_instance=cve_instance,
            registered_tasks=registered_tasks,
        )
        custom_prompt = self._strategy.build_manager_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt (no SEC-bench logic here - follows SRP)
        ctx = self._task_context(task_description, agent_id, agent_role, parent_task)
        limits = self._limits_context(execution_context)
        tool = self.default_tool

        return (
            self.chain()
            .render("core/roles/manager.j2", default_tool=tool)
            .with_security("manager_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool, **limits)
            .render("core/context/task.j2", **ctx, registered_tasks=registered_tasks)
            .render("core/output/subtasks.j2", default_tool=tool, **limits)
            .build()
        )

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        cve_instance: "CVEInstance | None" = None,
        registered_tasks: "list[RegisteredTask] | None" = None,
        execution_context: "ExecutionContext | None" = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation.

        Args:
            task_description: The task to delegate.
            agent_id: Current agent's ID.
            parent_task: Parent task description.
            cve_instance: CVE instance for benchmark runs.
            registered_tasks: List of already-registered tasks (for dedup hint).
            execution_context: Execution context with limits.
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.BOSS,
            default_tool=self.default_tool,
            parent_task=parent_task,
            cve_instance=cve_instance,
            registered_tasks=registered_tasks,
            execution_context=execution_context,
        )
        custom_prompt = self._strategy.build_boss_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt
        ctx = self._task_context(task_description, agent_id, AgentRole.BOSS, parent_task)
        limits = self._limits_context(execution_context)
        tool = self.default_tool

        return (
            self.chain()
            .render("core/roles/boss.j2", default_tool=tool)
            .with_security("boss_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool, **limits)
            .render("core/context/task.j2", **ctx, registered_tasks=registered_tasks)
            .render("core/output/subtasks.j2", default_tool=tool, **limits)
            .build()
        )

    def build_worker_prompt(
        self,
        task_description: str,
        sibling_context: Any = None,
        workspace_context: str | None = None,
        cve_instance: "CVEInstance | None" = None,
        parent_context: "ParentContext | None" = None,
    ) -> str:
        """Build prompt for WORKER agent task execution.

        Args:
            task_description: The task to execute.
            sibling_context: Context from sibling workers (completed tasks).
            workspace_context: Files in the workspace.
            cve_instance: CVE instance for benchmark runs.
            parent_context: Parent context for hierarchy info.
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=UUID("00000000-0000-0000-0000-000000000000"),  # Worker doesn't need ID
            agent_role=AgentRole.WORKER,
            default_tool=self.default_tool,
            cve_instance=cve_instance,
            parent_context=parent_context,
            sibling_context=sibling_context,
            workspace_context=workspace_context,
        )
        custom_prompt = self._strategy.build_worker_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt
        sibling_ctx = sibling_context.to_template_dict() if sibling_context else {}

        return (
            self.chain()
            .render_if(sibling_context, "core/context/sibling.j2", **sibling_ctx)
            .render("core/roles/worker.j2")
            .render("core/worker/execution.j2")
            .text(f"<TASK>\n{task_description}\n</TASK>")
            .render_if(workspace_context, "core/context/workspace.j2", files=workspace_context)
            .build()
        )
