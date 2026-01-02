"""Domain service for building hierarchical prompts from Jinja2 templates."""



from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.values.enums import AgentRole
from core.application.services.prompt_strategy import DefaultPromptStrategy, PromptContext, PromptStrategy

if TYPE_CHECKING:
    from core.application.services.context_composer import ContextComposer
    from core.domain.values.context import HierarchyLimits, SpawnPayload
    from core.domain.values.cve_instance import CVEInstance


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

    def with_context(self, context: "ContextComposer") -> Self:
        """Render context templates based on composed context.

        Renders templates for each context type present in the composer:
        - parent_summary -> core/context/parent.j2
        - ancestry_chain -> core/context/ancestry.j2
        - sibling_results -> core/context/siblings.j2
        - shared_decisions -> core/context/decisions.j2
        - shared_artifacts -> core/context/artifacts.j2
        - child_outcomes -> core/context/children.j2
        - ancestor_* and custom_* -> core/context/dynamic.j2

        Args:
            context: ContextComposer with added context data.

        Returns:
            Self for method chaining.
        """
        if not context:
            return self

        ctx_dict = context.build()

        # Render standard context templates
        self.render_if(context.has("parent_summary"), "core/context/parent.j2", **ctx_dict)
        self.render_if(context.has("ancestry_chain"), "core/context/ancestry.j2", **ctx_dict)
        self.render_if(context.has("sibling_results"), "core/context/siblings.j2", **ctx_dict)
        self.render_if(context.has("shared_decisions"), "core/context/decisions.j2", **ctx_dict)
        self.render_if(context.has("shared_artifacts"), "core/context/artifacts.j2", **ctx_dict)
        self.render_if(context.has("child_outcomes"), "core/context/children.j2", **ctx_dict)
        # Design Choice 4: Thinker Justification
        self.render_if(
            context.has("thinker_justification"),
            "core/context/thinker_justification.j2",
            **ctx_dict,
        )
        # Design Choice 5: Coworker Knowledge
        self.render_if(
            context.has("coworker_knowledge"),
            "core/context/coworker_knowledge.j2",
            **ctx_dict,
        )
        # Design Choice 6 & 7: Source Context
        self.render_if(
            context.has("source_context"),
            "core/context/source.j2",
            **ctx_dict,
        )

        # Render dynamic contexts (ancestor_* and custom_*)
        has_dynamic = context.has_any(
            *(k for k in context.keys() if k.startswith(("ancestor_", "custom_")))
        )
        if has_dynamic:
            self.render_optional("core/context/dynamic.j2", _context=ctx_dict)

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
        hierarchy_limits: "HierarchyLimits | None",
        complexity_budget: float | None = None,
        initial_complexity_budget: float | None = None,
        budget_threshold_ratio: float | None = None,
    ) -> dict[str, Any]:
        """Build limits context dict for templates.

        Args:
            hierarchy_limits: Current hierarchy limits for depth/children constraints.
            complexity_budget: Agent's current complexity budget (Design Choice 2-3).
            initial_complexity_budget: Initial budget allocated to this agent.
            budget_threshold_ratio: Ratio below which agent becomes WORKER.

        Returns:
            Dict with limit variables for templates:
            - max_subtasks: Max children allowed (None = no limit)
            - depth_remaining: Levels of hierarchy remaining (None = unlimited)
            - at_max_depth: True if children must be workers
            - agents_remaining: How many more agents can be created (None = unlimited)
            - max_total_agents: Maximum total agents allowed (None = unlimited)
            - current_total_agents: Current count of agents created (None = unknown)
            - complexity_budget: Current complexity budget (None = disabled)
            - initial_complexity_budget: Initial budget allocation (None = disabled)
            - budget_threshold: Minimum budget before forced WORKER (None = disabled)
        """
        base_context: dict[str, Any] = {
            "max_subtasks": None,
            "depth_remaining": None,
            "at_max_depth": False,
            "agents_remaining": None,
            "max_total_agents": None,
            "current_total_agents": None,
            # Budget context (Design Choices 2-3)
            "complexity_budget": complexity_budget,
            "initial_complexity_budget": initial_complexity_budget,
            "budget_threshold": None,
            "budget_enabled": complexity_budget is not None and complexity_budget > 0,
        }

        # Calculate budget threshold if budget is enabled
        if (
            complexity_budget is not None
            and initial_complexity_budget is not None
            and budget_threshold_ratio is not None
            and initial_complexity_budget > 0
        ):
            base_context["budget_threshold"] = initial_complexity_budget * budget_threshold_ratio

        if hierarchy_limits is None:
            return base_context

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

        base_context.update({
            "max_subtasks": max_subtasks,
            "depth_remaining": depth_remaining,
            "at_max_depth": at_max_depth,
            "agents_remaining": agents_remaining,
            "max_total_agents": max_total_agents,
            "current_total_agents": current_total_agents,
        })

        return base_context

    def _build_thinker_justification_context(
        self,
        spawn_payload: "SpawnPayload | None",
    ) -> dict[str, Any] | None:
        """Build thinker justification context from spawn payload.

        Extracts justification data from spawn_payload and builds
        ThinkerJustification context for template rendering.

        Args:
            spawn_payload: Spawn payload containing justification (Design Choice 4).

        Returns:
            Dict with thinker_justification for templates, or None if no justification.
        """
        if spawn_payload is None:
            return None

        justification = spawn_payload.subtask_justification
        if not justification:
            return None

        # Build thinker justification context
        from core.domain.values.subtask import SubtaskJustification
        from core.application.services.context_factories import (
            thinker_justification_from_subtask,
        )

        # Reconstruct SubtaskJustification from dict
        just_obj = SubtaskJustification(**justification)

        thinker_just = thinker_justification_from_subtask(
            thinker_task=spawn_payload.parent_task,
            justification=just_obj,
            child_budget=spawn_payload.complexity_budget if spawn_payload.complexity_budget > 0 else None,
            parent_budget=None,  # Not available in spawn_payload
            budget_weight=spawn_payload.budget_weight,
            total_weights=spawn_payload.total_weights,
            num_siblings=spawn_payload.num_siblings,
        )

        return {"thinker_justification": thinker_just.to_template_dict()}

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
        complexity_budget: float | None = None,
        initial_complexity_budget: float | None = None,
        budget_threshold_ratio: float | None = None,
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
            complexity_budget: Agent's current complexity budget (Design Choice 2-3).
            initial_complexity_budget: Initial budget allocated to this agent.
            budget_threshold_ratio: Ratio below which children become WORKER.
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
        limits = self._limits_context(
            hierarchy_limits,
            complexity_budget=complexity_budget,
            initial_complexity_budget=initial_complexity_budget,
            budget_threshold_ratio=budget_threshold_ratio,
        )
        tool = self.default_tool

        return (
            self.chain()
            .render("core/roles/manager.j2", default_tool=tool)
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
        complexity_budget: float | None = None,
        initial_complexity_budget: float | None = None,
        budget_threshold_ratio: float | None = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation.

        Args:
            task_description: The task to delegate.
            agent_id: Current agent's ID.
            parent_task: Parent task description.
            cve_instance: CVE instance for benchmark runs.
            hierarchy_limits: Hierarchy limits with depth/children constraints.
            complexity_budget: Agent's current complexity budget (Design Choice 2-3).
            initial_complexity_budget: Initial budget allocated to this agent.
            budget_threshold_ratio: Ratio below which children become WORKER.
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
        limits = self._limits_context(
            hierarchy_limits,
            complexity_budget=complexity_budget,
            initial_complexity_budget=initial_complexity_budget,
            budget_threshold_ratio=budget_threshold_ratio,
        )
        tool = self.default_tool

        return (
            self.chain()
            .render("core/roles/boss.j2", default_tool=tool)
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
            .render("core/worker/execution.j2")
            .text(f"<TASK>\n{task_description}\n</TASK>")
            .render_if(workspace_context, "core/context/workspace.j2", files=workspace_context)
            .build()
        )

    # -------------------------------------------------------------------------
    # Context-Aware Prompt Methods (using ContextComposer)
    # -------------------------------------------------------------------------

    def build_worker_prompt_with_context(
        self,
        task_description: str,
        context: "ContextComposer",
        workspace_context: str | None = None,
        cve_instance: "CVEInstance | None" = None,
    ) -> str:
        """Build worker prompt with composed context.

        This method uses the new ContextComposer API for flexible,
        programmatic context selection.

        Args:
            task_description: The task to execute.
            context: Composed context from ContextComposer.
            workspace_context: Optional workspace file listing.
            cve_instance: Optional CVE instance for benchmarks.

        Returns:
            Complete prompt string.

        Example:
            context = (
                ContextComposer()
                .add(parent_summary_from_agent(parent))
                .add(sibling_results_from_agents(siblings))
                .add(ancestor_data_from_agent(fixer, label="fixer"))
            )
            prompt = builder.build_worker_prompt_with_context(
                task_description=agent.task_description,
                context=context,
            )
        """
        return (
            self.chain()
            .with_context(context)
            .render("core/roles/worker.j2")
            .render("core/worker/execution.j2")
            .text(f"<TASK>\n{task_description}\n</TASK>")
            .render_if(workspace_context, "core/context/workspace.j2", files=workspace_context)
            .with_cve_context(cve_instance)
            .build()
        )

    def build_manager_prompt_with_context(
        self,
        task_description: str,
        context: "ContextComposer",
        hierarchy_limits: "HierarchyLimits | None" = None,
        cve_instance: "CVEInstance | None" = None,
    ) -> str:
        """Build manager prompt with composed context.

        This method uses the new ContextComposer API for flexible,
        programmatic context selection.

        Args:
            task_description: The task to decompose.
            context: Composed context from ContextComposer.
            hierarchy_limits: Hierarchy limits for depth/children constraints.
            cve_instance: Optional CVE instance for benchmarks.

        Returns:
            Complete prompt string.

        Example:
            context = (
                ContextComposer()
                .add(parent_summary_from_agent(parent))
                .add(ancestry_chain_from_agents(ancestors))
            )
            prompt = builder.build_manager_prompt_with_context(
                task_description=agent.task_description,
                context=context,
                hierarchy_limits=agent.hierarchy_limits,
            )
        """
        limits = self._limits_context(hierarchy_limits)
        tool = self.default_tool

        return (
            self.chain()
            .with_context(context)
            .render("core/roles/manager.j2", default_tool=tool)
            .with_security("manager_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool, **limits)
            .text(f"<TASK_TO_DECOMPOSE>\n{task_description}\n</TASK_TO_DECOMPOSE>")
            .render("core/output/subtasks.j2", default_tool=tool, **limits)
            .with_cve_context(cve_instance)
            .build()
        )

    def build_boss_prompt_with_context(
        self,
        task_description: str,
        context: "ContextComposer",
        hierarchy_limits: "HierarchyLimits | None" = None,
        cve_instance: "CVEInstance | None" = None,
    ) -> str:
        """Build boss prompt with composed context.

        This method uses the new ContextComposer API for flexible,
        programmatic context selection.

        Args:
            task_description: The task to delegate.
            context: Composed context from ContextComposer.
            hierarchy_limits: Hierarchy limits for depth/children constraints.
            cve_instance: Optional CVE instance for benchmarks.

        Returns:
            Complete prompt string.
        """
        limits = self._limits_context(hierarchy_limits)
        tool = self.default_tool

        return (
            self.chain()
            .with_context(context)
            .render("core/roles/boss.j2", default_tool=tool)
            .with_security("boss_overlay.j2", default_tool=tool)
            .render("core/strategies/decomposition.j2", default_tool=tool, **limits)
            .text(f"<TASK_TO_DELEGATE>\n{task_description}\n</TASK_TO_DELEGATE>")
            .render("core/output/subtasks.j2", default_tool=tool, **limits)
            .with_cve_context(cve_instance)
            .build()
        )

    # -------------------------------------------------------------------------
    # Source Context Extraction (Design Choice 6 & 7)
    # -------------------------------------------------------------------------

    def build_source_context_extraction_prompt(
        self,
        task_description: str,
    ) -> str:
        """Build prompt for extracting source context from task description.

        Uses LLM to parse unstructured user input and extract structured
        key information (DC6) and inferred CWE patterns (DC7).

        Args:
            task_description: The original task description from user input.

        Returns:
            Complete prompt string for source context extraction.
        """
        return (
            self.chain()
            .render(
                "core/extraction/source_context_extraction.j2",
                task_description=task_description,
            )
            .build()
        )
