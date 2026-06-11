"""Domain service for building hierarchical prompts from Jinja2 templates.

4-tier prompt architecture:
  Tier 1 (system.j2)       - Global constraints, config reference
  Tier 2 (roles/*.j2)      - Per-role persona and responsibility
  Tier 3 (operations/*.j2) - Per-operation criteria and output format
  Tier 4 (domains/*.j2)    - Optional domain-specific context
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.application.services.prompt.cache_breakpoint import (
    CACHE_BREAKPOINT_MARKER,
    remove_cache_breakpoints,
)
from core.application.services.prompt.prompt_strategy import (
    PromptContext,
    PromptStrategy,
    SubtaskScope,
)
from core.domain.values.enums import AgentRole
from core.domain.values.prompt_capabilities import PromptCapabilities


# Appended to the flat-mode prompt when the tool policy allows Claude's
# Task subagent tool. Without this note, Claude rarely exercises Task, so
# toggling Task availability degenerates into noise unless the prompt is
# also adjusted.
FLAT_SUBAGENT_NOTE = (
    "Note: The Task subagent tool is available; use it at your discretion to "
    "delegate complex steps."
)


if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.node_message import Briefing


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

    def text_if(self, condition: Any, content: str | None) -> Self:
        if condition and content is not None:
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
    ) -> None:
        self.template_dir = Path(template_dir)
        self.default_tool = default_tool
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,  # noqa: S701 - plain text prompts, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._strategy: PromptStrategy | None = strategy
        self._user_prompt: str = ""
        self._domain_context: object | None = None

    @staticmethod
    def _normalize_prompt_text(text: str) -> str:
        return " ".join(text.split())

    def _maybe_user_prompt_block(
        self,
        *,
        task_description: str,
        agent_role: AgentRole,
        briefing: "Briefing | None" = None,
    ) -> str | None:
        """Render the root prompt only when it adds new information."""
        if not self._user_prompt:
            return None

        if self._normalize_prompt_text(self._user_prompt) == self._normalize_prompt_text(
            task_description
        ):
            return None

        if agent_role in (AgentRole.PENDING, AgentRole.MANAGER) and briefing is not None:
            return None

        return f"<user_prompt>\n{self._user_prompt}\n</user_prompt>"

    def set_run_context(
        self,
        user_prompt: str,
        domain_context: object | None = None,
    ) -> None:
        self._user_prompt = user_prompt
        self._domain_context = domain_context

    def chain(self) -> TemplateChain:
        return TemplateChain(self.env, "template")

    def _append_volatile_suffix(
        self,
        chain: TemplateChain,
        *,
        user_prompt: str | None = None,
        scope: SubtaskScope | None = None,
        sibling_ctx: dict[str, Any] | None = None,
        briefing: "Briefing | None" = None,
        task_description: str | None = None,
        workspace_context: str | None = None,
        shared_code_index: str | None = None,
    ) -> TemplateChain:
        """Append the per-task volatile suffix to a stable-prefix chain.

        The prompt is split into a stable prefix (system + role + operation +
        strategy-extended domain content) followed by this trailing block of
        per-task content (handoff, user prompt, scope, briefing, task,
        workspace). Keeping the stable prefix byte-identical across calls is
        what lets ``litellm_adapter`` realize prompt-cache hits on multi-turn
        loops and across agents on the same CVE.

        ``shared_code_index`` (pre-rendered by the shared-code provider) lands
        immediately before <task>: proximity is the mechanism that makes the
        model trust the already-provided files instead of re-reading them.
        """
        return (
            chain.text_if(user_prompt, user_prompt)
            .render_if(scope and scope.has_scope, "context/scope.j2", scope=scope)
            .render_if(sibling_ctx, "context/sibling.j2", **(sibling_ctx or {}))
            .render_if(briefing, "context/briefing.j2", briefing=briefing)
            .text_if(shared_code_index, shared_code_index)
            .text_if(
                task_description is not None,
                f"<task>\n    {task_description}\n</task>",
            )
            .text_if(
                workspace_context,
                f"<workspace>\n    Files in shared workspace:\n    "
                f"{workspace_context}\n</workspace>",
            )
        )

    def _limits_context(
        self,
        hierarchy_limits: "HierarchyLimits | None",
    ) -> dict[str, Any]:
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

    def _build_role_prompt(
        self,
        prompt_ctx: PromptContext,
        *,
        role_template: str,
        operation_template: str,
        extender: Callable[[TemplateChain, PromptContext], "TemplateChain | None"] | None,
        operation_variable_template: str | None = None,
        insert_cache_breakpoint: bool = False,
    ) -> str:
        """Compose system + role + operation + (strategy extend) + volatile suffix.

        Template-method skeleton shared by assessment / boss / manager / worker
        prompts. The variations among them collapse to: which role+operation
        templates to render, and which strategy extender to invoke. Everything
        else (PromptContext fields, the volatile suffix, the chain.build) lives
        here.

        ``insert_cache_breakpoint`` marks the seam between the stable prefix
        and the volatile suffix with ``CACHE_BREAKPOINT_MARKER`` so the LLM
        adapter can place a single Anthropic ``cache_control`` breakpoint there.
        Only the LLM-routed prompts (assess/boss/manager) set this; the worker
        prompt goes to the worker SDK, not litellm, so it never carries the
        marker.

        ``operation_variable_template`` splits the operation across the seam:
        ``operation_template`` (static guidance — no live counters) renders into
        the cached prefix, while ``operation_variable_template`` (the
        depth/agent-limit conditionals and the output-format block) renders into
        the uncached tail. This keeps the cached prefix byte-identical even as
        the live hierarchy counters change per step. When ``None`` (worker), the
        single ``operation_template`` renders inline as before.
        """
        limits = self._limits_context(prompt_ctx.hierarchy_limits)
        capabilities = prompt_ctx.prompt_capabilities
        sibling_ctx = (
            prompt_ctx.handoff.to_template_dict() if prompt_ctx.handoff is not None else None
        )
        chain = (
            self.chain()
            .render(
                "system.j2",
                default_tool=prompt_ctx.default_tool,
                agent_role=prompt_ctx.agent_role.value,
            )
            .render(role_template)
            .render(
                operation_template,
                default_tool=prompt_ctx.default_tool,
                has_tools=(capabilities.has_tools if capabilities is not None else False),
                available_tools=(capabilities.available_tools if capabilities is not None else ()),
                **limits,
            )
        )
        if extender is not None:
            extended = extender(chain, prompt_ctx)
            chain = extended if extended is not None else chain

        def append_volatile(target: TemplateChain) -> TemplateChain:
            return self._append_volatile_suffix(
                target,
                user_prompt=self._maybe_user_prompt_block(
                    task_description=prompt_ctx.task_description,
                    agent_role=prompt_ctx.agent_role,
                    briefing=prompt_ctx.briefing,
                ),
                scope=prompt_ctx.scope,
                sibling_ctx=sibling_ctx,
                briefing=prompt_ctx.briefing,
                task_description=prompt_ctx.task_description,
                workspace_context=prompt_ctx.workspace_context,
                shared_code_index=prompt_ctx.shared_code_index,
            )

        if not insert_cache_breakpoint:
            return append_volatile(chain).build()

        # Cache-optimized path: assemble the stable prefix and the volatile tail
        # as separate strings, then join with exactly ONE seam marker. Any stray
        # marker that data-controlled content (CVE text, task, briefing) might
        # contain is scrubbed from each side, so the seam is unambiguous and the
        # cached prefix can never be truncated by an injected marker. For normal
        # prompts (no stray markers) this is byte-identical to a single render.
        static_text = remove_cache_breakpoints(chain.build())
        variable_chain = self.chain()
        if operation_variable_template is not None:
            variable_chain = variable_chain.render(
                operation_variable_template,
                default_tool=prompt_ctx.default_tool,
                **limits,
            )
        variable_text = remove_cache_breakpoints(append_volatile(variable_chain).build())
        if not variable_text:
            return static_text
        return f"{static_text}\n\n{CACHE_BREAKPOINT_MARKER}\n\n{variable_text}"

    def build_assessment_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        briefing: "Briefing | None" = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
        domain_context: object | None = None,
        scope: SubtaskScope | None = None,
        prompt_capabilities: PromptCapabilities | None = None,
    ) -> str:
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.PENDING,
            default_tool=self.default_tool,
            briefing=briefing,
            hierarchy_limits=hierarchy_limits,
            domain_context=domain_context,
            scope=scope,
            prompt_capabilities=prompt_capabilities,
        )
        return self._build_role_prompt(
            prompt_ctx,
            role_template="roles/pending.j2",
            operation_template="operations/assess_static.j2",
            operation_variable_template="operations/assess_variable.j2",
            extender=(
                self._strategy.extend_assessment_prompt if self._strategy is not None else None
            ),
            insert_cache_breakpoint=True,
        )

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        domain_context: object | None = None,
        hierarchy_limits: "HierarchyLimits | None" = None,
        prompt_capabilities: PromptCapabilities | None = None,
    ) -> str:
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.BOSS,
            default_tool=self.default_tool,
            parent_task=parent_task,
            domain_context=domain_context,
            hierarchy_limits=hierarchy_limits,
            prompt_capabilities=prompt_capabilities,
        )
        return self._build_role_prompt(
            prompt_ctx,
            role_template="roles/boss.j2",
            operation_template="operations/decomposition_static.j2",
            operation_variable_template="operations/decomposition_variable.j2",
            extender=self._strategy.extend_boss_prompt if self._strategy is not None else None,
            insert_cache_breakpoint=True,
        )

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
        prompt_capabilities: PromptCapabilities | None = None,
    ) -> str:
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
            prompt_capabilities=prompt_capabilities,
        )
        return self._build_role_prompt(
            prompt_ctx,
            role_template="roles/manager.j2",
            operation_template="operations/decomposition_static.j2",
            operation_variable_template="operations/decomposition_variable.j2",
            extender=(self._strategy.extend_manager_prompt if self._strategy is not None else None),
            insert_cache_breakpoint=True,
        )

    def build_flat_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        domain_context: object | None = None,
        subagent_enabled: bool = False,
    ) -> str:
        """Build the flat-mode (single-agent) prompt.

        Delegates to the active domain strategy's ``extend_flat_prompt``,
        optionally appends ``FLAT_SUBAGENT_NOTE`` (when the tool policy
        enables Claude's ``Task``), and appends the task slug. When no
        strategy or no domain context is supplied, the prompt collapses to
        the note (when enabled) plus the task slug.
        """
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id,
            agent_role=AgentRole.WORKER,
            default_tool=self.default_tool,
            domain_context=domain_context,
        )
        chain = self.chain()
        extended_chain = (
            self._strategy.extend_flat_prompt(chain, prompt_ctx)
            if self._strategy is not None
            else None
        )
        body = (extended_chain or chain).build()
        task_block = f"<task>\n{task_description}\n</task>"
        parts = [p for p in (body, FLAT_SUBAGENT_NOTE if subagent_enabled else "", task_block) if p]
        return "\n\n".join(parts)

    def build_worker_prompt(
        self,
        task_description: str,
        agent_id: UUID | None = None,
        handoff: Any = None,
        workspace_context: str | None = None,
        domain_context: object | None = None,
        briefing: "Briefing | None" = None,
        shared_code_block: str | None = None,
        shared_code_index: str | None = None,
    ) -> str:
        prompt_ctx = PromptContext(
            task_description=task_description,
            agent_id=agent_id or UUID("00000000-0000-0000-0000-000000000000"),
            agent_role=AgentRole.WORKER,
            default_tool=self.default_tool,
            domain_context=domain_context,
            briefing=briefing,
            handoff=handoff,
            workspace_context=workspace_context,
            shared_code_block=shared_code_block,
            shared_code_index=shared_code_index,
        )
        return self._build_role_prompt(
            prompt_ctx,
            role_template="roles/worker.j2",
            operation_template="operations/execution.j2",
            extender=(self._strategy.extend_worker_prompt if self._strategy is not None else None),
        )
