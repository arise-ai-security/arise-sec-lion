"""Domain service for building hierarchical prompts from Jinja2 templates."""



from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.values.enums import AgentRole
from core.application.services.prompt_strategy import DefaultPromptStrategy, PromptContext, PromptStrategy

if TYPE_CHECKING:
    from core.domain.values.context import HierarchyLimits, SpawnPayload
    from core.domain.values.cve_instance import CVEInstance

_CVE_DISPLAY_FIELDS = (
    "instance_id",
    "cve_id",
    "repo",
    "project_name",
    "lang",
    "sanitizer",
    "base_commit",
    "work_dir",
    "bug_description",
    "candidate_fixes",
)


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

    def with_user_prompt(self, user_prompt: str) -> Self:
        """Inject user prompt context (generic, reusable).

        Renders core/context/user_prompt.j2 with the user's original input.
        Independent of SEC-bench - can be used in any context.

        Args:
            user_prompt: Original user task description from CLI.
        """
        if user_prompt:
            return self.render_optional("core/context/user_prompt.j2", user_prompt=user_prompt)
        return self

    def with_cve_display(
        self,
        cve_instance: "CVEInstance | None",
        display_fields: tuple[str, ...] | None = None,
    ) -> Self:
        """Inject SEC-bench CVE display context.

        Renders secbench/cve-context.j2 with CVE instance data.
        Independent of user_prompt - use with_user_prompt() separately.

        Args:
            cve_instance: CVE instance data (optional).
            display_fields: Fields to include in cve dict for iteration.
                          Defaults to standard display fields.
        """
        if cve_instance is None:
            return self

        fields = display_fields or _CVE_DISPLAY_FIELDS
        cve_ctx = cve_instance.to_template_context()
        cve_display = {k: v for k, v in cve_ctx.items() if k in fields and v}

        return self.with_secbench("cve-context.j2", cve=cve_display, **cve_ctx)

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
        # Run-level context (set once at start, used by all agents)
        self._user_prompt: str = ""
        self._cve_instance: "CVEInstance | None" = None

    def set_run_context(
        self,
        user_prompt: str,
        cve_instance: "CVEInstance | None" = None,
    ) -> None:
        """Set global context for this run (called once at start).

        Args:
            user_prompt: Original user task description from CLI.
            cve_instance: CVE instance for benchmark runs.
        """
        self._user_prompt = user_prompt
        self._cve_instance = cve_instance

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
        hierarchy_limits: "HierarchyLimits | None",
    ) -> dict[str, Any]:
        """Build limits context dict for templates.

        Args:
            hierarchy_limits: Current hierarchy limits for depth/children constraints.

        Returns:
            Dict with limit variables for templates:
            - max_subtasks: Max children allowed (None = no limit)
            - depth_remaining: Levels of hierarchy remaining (None = unlimited)
            - at_max_depth: True if children must be workers
            - agents_remaining: How many more agents can be created (None = unlimited)
            - max_total_agents: Maximum total agents allowed (None = unlimited)
            - current_total_agents: Current count of agents created (None = unknown)
        """
        if hierarchy_limits is None:
            return {
                "max_subtasks": None,
                "depth_remaining": None,
                "at_max_depth": False,
                "agents_remaining": None,
                "max_total_agents": None,
                "current_total_agents": None,
            }

        max_subtasks = None
        if hierarchy_limits.is_children_limited():
            max_subtasks = hierarchy_limits.max_children_per_node

        depth_remaining = None
        at_max_depth = False
        if hierarchy_limits.is_depth_limited():
            depth_remaining = hierarchy_limits.max_depth - hierarchy_limits.current_depth
            at_max_depth = not hierarchy_limits.can_spawn_child()

        # Total agents limit info
        agents_remaining = None
        max_total_agents = None
        current_total_agents = None
        if hierarchy_limits.is_total_agents_limited():
            agents_remaining = hierarchy_limits.agents_remaining()
            max_total_agents = hierarchy_limits.max_total_agents
            current_total_agents = hierarchy_limits.current_total_agents

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
        cve_instance: "CVEInstance | None" = None,
        spawn_payload: "SpawnPayload | None" = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition.

        Args:
            task_description: The task to decompose.
            agent_id: Current agent's ID.
            agent_role: Agent role (should be MANAGER).
            parent_task: Parent task description.
            cve_instance: CVE instance for benchmark runs.
            spawn_payload: Spawn payload for hierarchy info.
            hierarchy_limits: Hierarchy limits with depth/children constraints.
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=agent_role,
            default_tool=self.default_tool,
            parent_task=parent_task,
            spawn_payload=spawn_payload,
            hierarchy_limits=hierarchy_limits,
            cve_instance=cve_instance,
        )
        custom_prompt = self._strategy.build_manager_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt (no SEC-bench logic here - follows SRP)
        ctx = self._task_context(task_description, agent_id, agent_role, parent_task)
        limits = self._limits_context(hierarchy_limits)
        tool = self.default_tool

        return (
            self.chain()
            .render("core/roles/manager.j2", default_tool=tool)
            .with_user_prompt(self._user_prompt)
            .with_cve_display(self._cve_instance)
            .with_security("manager_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool, **limits)
            .render("core/context/task.j2", **ctx)
            .render("core/output/subtasks.j2", default_tool=tool, **limits)
            .build()
        )

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        cve_instance: "CVEInstance | None" = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation.

        Args:
            task_description: The task to delegate.
            agent_id: Current agent's ID.
            parent_task: Parent task description.
            cve_instance: CVE instance for benchmark runs.
            hierarchy_limits: Hierarchy limits with depth/children constraints.
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.BOSS,
            default_tool=self.default_tool,
            parent_task=parent_task,
            cve_instance=cve_instance,
            hierarchy_limits=hierarchy_limits,
        )
        custom_prompt = self._strategy.build_boss_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt
        ctx = self._task_context(task_description, agent_id, AgentRole.BOSS, parent_task)
        limits = self._limits_context(hierarchy_limits)
        tool = self.default_tool

        return (
            self.chain()
            .render("core/roles/boss.j2", default_tool=tool)
            .with_user_prompt(self._user_prompt)
            .with_cve_display(self._cve_instance)
            .with_security("boss_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool, **limits)
            .render("core/context/task.j2", **ctx)
            .render("core/output/subtasks.j2", default_tool=tool, **limits)
            .build()
        )

    def build_worker_prompt(
        self,
        task_description: str,
        sibling_view: Any = None,
        workspace_context: str | None = None,
        cve_instance: "CVEInstance | None" = None,
        spawn_payload: "SpawnPayload | None" = None,
    ) -> str:
        """Build prompt for WORKER agent task execution.

        Args:
            task_description: The task to execute.
            sibling_view: View of sibling workers (completed tasks).
            workspace_context: Files in the workspace.
            cve_instance: CVE instance for benchmark runs.
            spawn_payload: Spawn payload for hierarchy info.
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=UUID("00000000-0000-0000-0000-000000000000"),  # Worker doesn't need ID
            agent_role=AgentRole.WORKER,
            default_tool=self.default_tool,
            cve_instance=cve_instance,
            spawn_payload=spawn_payload,
            sibling_view=sibling_view,
            workspace_context=workspace_context,
        )
        custom_prompt = self._strategy.build_worker_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt
        sibling_ctx = sibling_view.to_template_dict() if sibling_view else {}

        return (
            self.chain()
            .render_if(sibling_view, "core/context/sibling.j2", **sibling_ctx)
            .render("core/roles/worker.j2")
            .with_user_prompt(self._user_prompt)
            .with_cve_display(self._cve_instance)
            .render("core/worker/execution.j2")
            .text(f"<TASK>\n{task_description}\n</TASK>")
            .render_if(workspace_context, "core/context/workspace.j2", files=workspace_context)
            .build()
        )
