"""Domain service for building hierarchical prompts from Jinja2 templates.

4-tier prompt architecture:
  Tier 1 (system.j2)     — Global constraints, config reference
  Tier 2 (roles/*.j2)    — Per-role persona and responsibility
  Tier 3 (operations/*.j2) — Per-operation criteria and output format
  Tier 4 (domains/*.j2)  — Domain-specific context (e.g., SEC-bench)
"""



from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.values.enums import AgentRole
from core.application.services.prompt_strategy import DefaultPromptStrategy, PromptContext, PromptStrategy

if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.node_message import Briefing
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

    def with_cve_display(
        self,
        cve_instance: "CVEInstance | None",
        display_fields: tuple[str, ...] | None = None,
        phase: str | None = None,
    ) -> Self:
        """Inject domain CVE context if cve_instance is present.

        Args:
            cve_instance: CVE instance data (optional).
            display_fields: Fields to include in cve dict for iteration.
            phase: Phase filter ('builder', 'exploiter', 'fixer', or None for all).
        """
        if cve_instance is None:
            return self

        fields = display_fields or _CVE_DISPLAY_FIELDS
        cve_ctx = cve_instance.to_template_context()
        cve_display = {k: v for k, v in cve_ctx.items() if k in fields and v}

        return self.render("domains/secbench/cve.j2", cve=cve_display, phase=phase, **cve_ctx)

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
    """Compose hierarchical prompts using 4-tier architecture.

    Uses Strategy Pattern to delegate specialized prompt building
    (e.g., SEC-bench) to injected strategies, following SRP.
    """

    def __init__(
        self,
        template_dir: str | Path,
        default_tool: str,
        strategy: PromptStrategy | None = None,
    ) -> None:
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
        """Set global context for this run (called once at start)."""
        self._user_prompt = user_prompt
        self._cve_instance = cve_instance

    def chain(self) -> TemplateChain:
        """Create a new template chain builder."""
        return TemplateChain(self.env, "template")

    def set_strategy(self, strategy: PromptStrategy) -> None:
        """Set the prompt strategy after initialization."""
        self._strategy = strategy

    def _limits_context(
        self,
        hierarchy_limits: "HierarchyLimits | None",
    ) -> dict[str, Any]:
        """Build limits context dict for templates."""
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

    def build_assessment_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        briefing: "Briefing | None" = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
        cve_instance: "CVEInstance | None" = None,
    ) -> str:
        """Build prompt for PENDING agent task assessment.

        Single prompt that decides: execute directly or decompose.
        Tiers: system → roles/pending → user_prompt → [domain] → operations/assess
        """
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.PENDING,
            default_tool=self.default_tool,
            briefing=briefing,
            hierarchy_limits=hierarchy_limits,
            cve_instance=cve_instance,
        )
        custom_prompt = self._strategy.build_assessment_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        limits = self._limits_context(hierarchy_limits)

        return (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/pending.j2")
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .with_cve_display(self._cve_instance)
            .render(
                "operations/assess.j2",
                task_description=task_description,
                briefing=briefing,
                default_tool=self.default_tool,
                **limits,
            )
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

        Tiers: system → roles/boss → user_prompt → [domain] → operations/decomposition
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
        limits = self._limits_context(hierarchy_limits)

        return (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/boss.j2")
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .with_cve_display(self._cve_instance)
            .render(
                "operations/decomposition.j2",
                task_description=task_description,
                briefing=None,
                default_tool=self.default_tool,
                **limits,
            )
            .build()
        )

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole = AgentRole.MANAGER,
        parent_task: str | None = None,
        cve_instance: "CVEInstance | None" = None,
        briefing: "Briefing | None" = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition.

        Tiers: system → roles/manager → user_prompt → [domain] → operations/decomposition
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=agent_role,
            default_tool=self.default_tool,
            parent_task=parent_task,
            briefing=briefing,
            hierarchy_limits=hierarchy_limits,
            cve_instance=cve_instance,
        )
        custom_prompt = self._strategy.build_manager_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt
        limits = self._limits_context(hierarchy_limits)

        return (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/manager.j2")
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .with_cve_display(self._cve_instance)
            .render(
                "operations/decomposition.j2",
                task_description=task_description,
                briefing=briefing,
                default_tool=self.default_tool,
                **limits,
            )
            .build()
        )

    def build_worker_prompt(
        self,
        task_description: str,
        handoff: Any = None,
        workspace_context: str | None = None,
        cve_instance: "CVEInstance | None" = None,
        briefing: "Briefing | None" = None,
    ) -> str:
        """Build prompt for WORKER agent task execution.

        Tiers: system → roles/worker → [sibling] → user_prompt → [domain] → operations/execution
        """
        # Try strategy first (for SEC-bench or other specialized prompts)
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=UUID("00000000-0000-0000-0000-000000000000"),  # Worker doesn't need ID
            agent_role=AgentRole.WORKER,
            default_tool=self.default_tool,
            cve_instance=cve_instance,
            briefing=briefing,
            handoff=handoff,
            workspace_context=workspace_context,
        )
        custom_prompt = self._strategy.build_worker_prompt(prompt_ctx)
        if custom_prompt is not None:
            return custom_prompt

        # Default generic prompt
        sibling_ctx = handoff.to_template_dict() if handoff else {}

        return (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/worker.j2")
            .render_if(handoff, "context/sibling.j2", **sibling_ctx)
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .with_cve_display(self._cve_instance)
            .render(
                "operations/execution.j2",
                task_description=task_description,
                workspace_context=workspace_context,
            )
            .build()
        )
