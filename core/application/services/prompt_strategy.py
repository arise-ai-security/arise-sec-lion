"""Prompt strategy protocol and implementations for different use cases.

This module implements the Strategy Pattern to separate SEC-bench specific
prompt logic from generic tree prompts, following SRP and OCP principles.
"""



from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol
from uuid import UUID

from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.node_message import Briefing
    from core.domain.values.cve_instance import CVEInstance
    from core.application.services.prompt_builder import TemplateChain


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Immutable context for prompt strategies."""

    task_description: str
    agent_id: UUID
    agent_role: AgentRole
    default_tool: str
    parent_task: str | None = None
    briefing: "Briefing | None" = None
    hierarchy_limits: "HierarchyLimits | None" = None
    cve_instance: "CVEInstance | None" = None
    handoff: Any = None
    workspace_context: str | None = None


class PromptStrategy(Protocol):
    """Strategy interface for prompt building.

    Implementations return custom prompts for specific use cases,
    or None to fall back to default prompt building.
    """

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        """Return custom boss prompt, or None to use default."""
        ...

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        """Return custom manager prompt, or None to use default."""
        ...

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        """Return custom worker prompt, or None to use default."""
        ...

    def build_assessment_prompt(self, context: PromptContext) -> str | None:
        """Return custom assessment prompt, or None to use default."""
        ...


class DefaultPromptStrategy:
    """Default strategy - returns None to use base prompts for all roles."""

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_assessment_prompt(self, context: PromptContext) -> str | None:
        return None


def _detect_benchmark_branch(briefing: "Briefing | None") -> str | None:
    """Detect which SEC-bench branch (builder/exploiter/fixer) this agent belongs to.

    Checks ancestry chain for top-level subtask keywords.
    Returns None if not in a benchmark run or branch not determinable.
    """
    if briefing is None:
        return None

    for ancestor in briefing.ancestry:
        task_lower = ancestor.task_summary.lower()
        if any(kw in task_lower for kw in ("builder", "environment", "setup", "docker pull")):
            return "builder"
        if any(kw in task_lower for kw in ("exploiter", "poc", "exploit", "proof of concept")):
            return "exploiter"
        if any(kw in task_lower for kw in ("fixer", "patch", "fix")):
            return "fixer"

    return None


def _detect_branch_from_task(task_description: str) -> str | None:
    """Detect SEC-bench branch from task description keywords."""
    task_lower = task_description.lower()
    if any(kw in task_lower for kw in ("[builder]", "builder", "environment", "setup")):
        return "builder"
    if any(kw in task_lower for kw in ("[exploiter]", "exploiter", "poc", "exploit")):
        return "exploiter"
    if any(kw in task_lower for kw in ("[fixer]", "fixer", "patch", "fix")):
        return "fixer"
    return None


class SecBenchPromptStrategy:
    """SEC-bench specific prompt strategy.

    Provides specialized prompts for CVE benchmark runs with
    builder/exploiter/fixer phase detection.

    Uses 4-tier template architecture:
      system.j2 → roles/*.j2 → domains/secbench/*.j2
    """

    def __init__(self, chain_factory: "Callable[[], TemplateChain]") -> None:
        self._chain_factory = chain_factory

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench boss prompt if CVE instance is present."""
        if context.cve_instance is None:
            return None

        cve_ctx = context.cve_instance.to_template_context()
        return (
            self._chain_factory()
            .render("system.j2", default_tool=context.default_tool)
            .render("roles/boss.j2")
            .text(f"<user_prompt>\n{context.task_description}\n</user_prompt>")
            .with_cve_display(context.cve_instance, phase=None)
            .render("domains/secbench/boss.j2", **cve_ctx, default_tool=context.default_tool)
            .build()
        )

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench manager prompt for top-level phase managers only.

        SEC-bench manager templates are ONLY for depth-1 managers (direct BOSS children)
        e.g., [Builder], [Exploiter], [Fixer] - NOT for nested managers like [Builder-5].
        """
        if context.cve_instance is None:
            return None

        # Detect branch from ancestry or task description
        branch = _detect_benchmark_branch(context.briefing)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)

        if branch is None:
            return None

        # Only apply to direct BOSS children (depth-1 managers)
        is_direct_boss_child = (
            context.briefing is not None and len(context.briefing.ancestry) == 1
        )

        if not is_direct_boss_child:
            return None  # Use default prompt for nested managers

        cve_ctx = context.cve_instance.to_template_context()
        return (
            self._chain_factory()
            .render("system.j2", default_tool=context.default_tool)
            .render("roles/manager.j2")
            .text(f"<user_prompt>\n{context.task_description}\n</user_prompt>")
            .with_cve_display(context.cve_instance, phase=branch)
            .render_if(
                branch == "builder",
                "domains/secbench/manager/builder.j2",
                **cve_ctx,
                default_tool=context.default_tool,
            )
            .render_if(
                branch == "exploiter",
                "domains/secbench/manager/exploiter.j2",
                **cve_ctx,
                default_tool=context.default_tool,
            )
            .render_if(
                branch == "fixer",
                "domains/secbench/manager/fixer.j2",
                **cve_ctx,
                default_tool=context.default_tool,
            )
            .build()
        )

    def build_assessment_prompt(self, context: PromptContext) -> str | None:
        return None  # SEC-bench uses default confidence-based assessment

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench worker prompt if in a benchmark branch."""
        if context.cve_instance is None:
            return None

        branch = _detect_benchmark_branch(context.briefing)
        if branch is None:
            return None

        sibling_ctx = (
            context.handoff.to_template_dict()
            if context.handoff
            else {}
        )
        cve_ctx = context.cve_instance.to_template_context()

        return (
            self._chain_factory()
            .render("system.j2", default_tool=context.default_tool)
            .render("roles/worker.j2")
            .text(f"<user_prompt>\n{context.task_description}\n</user_prompt>")
            .with_cve_display(context.cve_instance, phase=branch)
            .render_if(context.handoff, "context/sibling.j2", **sibling_ctx)
            .render_if(branch == "builder", "domains/secbench/worker/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "domains/secbench/worker/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "domains/secbench/worker/fixer.j2", **cve_ctx)
            .build()
        )
