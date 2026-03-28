"""Domain service for building hierarchical prompts from Jinja2 templates.

4-tier prompt architecture:
  Tier 1 (system.j2)       - Global constraints, config reference
  Tier 2 (roles/*.j2)      - Per-role persona and responsibility
  Tier 3 (operations/*.j2) - Per-operation criteria and output format
  Tier 4 (domains/*.j2)    - Optional domain-specific context
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.application.services.prompt_strategy import (
    PromptContext,
    PromptStrategy,
    SubtaskScope,
)
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.node_message import Briefing
    from core.ports.domain_plugin_port import DomainPlugin


class TemplateChain:
    """Fluent builder for chaining template renders."""

    __slots__ = ("_env", "_error_context", "_parts")

    def __init__(
        self,
        env: Environment,
        error_context: str = "template",
    ) -> None:
        self._env = env
        self._parts: list[str] = []
        self._error_context = error_context

    def _template_exists(self, name: str) -> bool:
        try:
            self._env.get_template(name)
            return True
        except TemplateNotFound:
            return False

    def render(self, template: str, **kwargs: Any) -> Self:
        try:
            self._parts.append(self._env.get_template(template).render(**kwargs))
        except TemplateNotFound as e:
            raise TemplateNotFound(f"Required {self._error_context} not found: {e.name}") from e
        return self

    def render_if(self, condition: Any, template: str, **kwargs: Any) -> Self:
        if condition:
            return self.render(template, **kwargs)
        return self

    def render_optional(self, template: str, **kwargs: Any) -> Self:
        if self._template_exists(template):
            self._parts.append(self._env.get_template(template).render(**kwargs))
        return self

    def text(self, content: str) -> Self:
        self._parts.append(content)
        return self

    def text_if(self, condition: Any, content: str) -> Self:
        if condition:
            self._parts.append(content)
        return self

    def build(self, separator: str = "\n\n") -> str:
        return separator.join(self._parts)


class PromptBuilder:
    """Compose hierarchical prompts using 4-tier architecture."""

    def __init__(
        self,
        template_dir: str | Path,
        default_tool: str,
        strategy: PromptStrategy | None = None,
        domain_plugin: "DomainPlugin | None" = None,
    ) -> None:
        self.template_dir = Path(template_dir)
        self.default_tool = default_tool
        self._domain_plugin = domain_plugin
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,  # noqa: S701 - plain text prompts, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._strategy: PromptStrategy | None = strategy
        self._user_prompt: str = ""
        self._domain_context: object | None = None

    def set_run_context(
        self,
        user_prompt: str,
        domain_context: object | None = None,
    ) -> None:
        """Set global context for this run (called once at start)."""
        self._user_prompt = user_prompt
        self._domain_context = domain_context

    def chain(self) -> TemplateChain:
        """Create a new template chain builder."""
        return TemplateChain(self.env, "template")

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
        domain_context: object | None = None,
        scope: SubtaskScope | None = None,
    ) -> str:
        """Build prompt for PENDING agent task assessment."""
        limits = self._limits_context(hierarchy_limits)
        return (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/pending.j2")
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .render_if(
                scope and scope.has_scope,
                "context/scope.j2",
                scope=scope,
            )
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
        domain_context: object | None = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation."""
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.BOSS,
            default_tool=self.default_tool,
            parent_task=parent_task,
            domain_context=domain_context,
            hierarchy_limits=hierarchy_limits,
        )
        limits = self._limits_context(hierarchy_limits)
        chain = (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/boss.j2")
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .render(
                "operations/decomposition.j2",
                task_description=task_description,
                briefing=None,
                default_tool=self.default_tool,
                **limits,
            )
        )
        extended_chain = (
            self._strategy.extend_boss_prompt(chain, prompt_ctx)
            if self._strategy is not None
            else None
        )
        return (extended_chain or chain).build()

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: AgentRole = AgentRole.MANAGER,
        parent_task: str | None = None,
        domain_context: object | None = None,
        briefing: "Briefing | None" = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
        scope: SubtaskScope | None = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition."""
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=agent_role,
            default_tool=self.default_tool,
            parent_task=parent_task,
            briefing=briefing,
            hierarchy_limits=hierarchy_limits,
            domain_context=domain_context,
            scope=scope,
        )
        limits = self._limits_context(hierarchy_limits)
        chain = (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/manager.j2")
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .render_if(
                scope and scope.has_scope,
                "context/scope.j2",
                scope=scope,
            )
            .render(
                "operations/decomposition.j2",
                task_description=task_description,
                briefing=briefing,
                default_tool=self.default_tool,
                **limits,
            )
        )
        extended_chain = (
            self._strategy.extend_manager_prompt(chain, prompt_ctx)
            if self._strategy is not None
            else None
        )
        return (extended_chain or chain).build()

    def _apply_domain_enrichment(
        self,
        prompt: str,
        *,
        domain_context: object | None = None,
        briefing: "Briefing | None" = None,
    ) -> str:
        if self._domain_plugin is None:
            return prompt

        active_domain_context = (
            domain_context if domain_context is not None else self._domain_context
        )
        return self._domain_plugin.enrich_prompt(
            prompt,
            domain_context=active_domain_context,
            briefing=briefing,
            chain_factory=self.chain,
        )

    def build_worker_prompt(
        self,
        task_description: str,
        handoff: Any = None,
        workspace_context: str | None = None,
        domain_context: object | None = None,
        briefing: "Briefing | None" = None,
    ) -> str:
        """Build prompt for WORKER agent task execution."""
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=UUID("00000000-0000-0000-0000-000000000000"),
            agent_role=AgentRole.WORKER,
            default_tool=self.default_tool,
            domain_context=domain_context,
            briefing=briefing,
            handoff=handoff,
            workspace_context=workspace_context,
        )
        sibling_ctx = handoff.to_template_dict() if handoff else {}
        chain = (
            self.chain()
            .render("system.j2", default_tool=self.default_tool)
            .render("roles/worker.j2")
            .render_if(handoff, "context/sibling.j2", **sibling_ctx)
            .text_if(self._user_prompt, f"<user_prompt>\n{self._user_prompt}\n</user_prompt>")
            .render(
                "operations/execution.j2",
                task_description=task_description,
                workspace_context=workspace_context,
            )
        )
        extended_chain = (
            self._strategy.extend_worker_prompt(chain, prompt_ctx)
            if self._strategy is not None
            else None
        )
        prompt = (extended_chain or chain).build()

        return self._apply_domain_enrichment(
            prompt,
            domain_context=domain_context,
            briefing=briefing,
        )
