"""Prompt strategy protocol and implementations for different use cases.

This module implements the Strategy Pattern to separate SEC-bench specific
prompt logic from generic tree prompts, following SRP and OCP principles.
"""



from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol
from uuid import UUID

from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.domain.values.context import HierarchyLimits, SpawnPayload
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
    spawn_payload: "SpawnPayload | None" = None
    hierarchy_limits: "HierarchyLimits | None" = None
    cve_instance: "CVEInstance | None" = None
    sibling_view: Any = None
    workspace_context: str | None = None
    research_findings: str | None = None


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

    def build_researcher_prompt(self, context: PromptContext) -> str | None:
        """Return custom researcher prompt, or None to use default."""
        ...



class DefaultPromptStrategy:
    """Default strategy - returns None to use base prompts for all roles."""

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_researcher_prompt(self, context: PromptContext) -> str | None:
        return None


def _detect_benchmark_branch(spawn_payload: "SpawnPayload | None") -> str | None:
    """Detect which SEC-bench branch (builder/exploiter/fixer) this agent belongs to.

    Checks ancestry chain for top-level subtask keywords.
    Returns None if not in a benchmark run or branch not determinable.
    """
    if spawn_payload is None:
        return None

    for ancestor in spawn_payload.ancestry:
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
    """

    def __init__(self, chain_factory: "Callable[[], TemplateChain]") -> None:
        """Initialize with a factory for creating template chains.

        Args:
            chain_factory: Callable that creates a new TemplateChain instance.
        """
        self._chain_factory = chain_factory

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench boss prompt if CVE instance is present."""
        if context.cve_instance is None:
            return None

        cve_ctx = context.cve_instance.to_template_context()
        return (
            self._chain_factory()
            .render("core/roles/boss.j2", default_tool=context.default_tool)
            .with_user_prompt(context.task_description)
            .with_cve_display(context.cve_instance)
            .render("secbench/boss.j2", **cve_ctx, default_tool=context.default_tool)
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
        branch = _detect_benchmark_branch(context.spawn_payload)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)

        if branch is None:
            return None

        # Only apply to direct BOSS children (depth-1 managers)
        # spawn_payload.depth == 0 means parent is BOSS
        is_direct_boss_child = (
            context.spawn_payload is not None and context.spawn_payload.depth == 0
        )

        if not is_direct_boss_child:
            return None  # Use default prompt for nested managers

        cve_ctx = context.cve_instance.to_template_context()
        return (
            self._chain_factory()
            .render("core/roles/manager.j2", default_tool=context.default_tool)
            .with_user_prompt(context.task_description)
            .with_cve_display(context.cve_instance)
            .render_if(
                branch == "builder",
                "secbench/manager/builder.j2",
                **cve_ctx,
                default_tool=context.default_tool,
            )
            .render_if(
                branch == "exploiter",
                "secbench/manager/exploiter.j2",
                **cve_ctx,
                default_tool=context.default_tool,
            )
            .render_if(
                branch == "fixer",
                "secbench/manager/fixer.j2",
                **cve_ctx,
                default_tool=context.default_tool,
            )
            .build()
        )

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench worker prompt if in a benchmark branch."""
        if context.cve_instance is None:
            return None

        branch = _detect_benchmark_branch(context.spawn_payload)
        if branch is None:
            return None

        sibling_ctx = (
            context.sibling_view.to_template_dict()
            if context.sibling_view
            else {}
        )
        cve_ctx = context.cve_instance.to_template_context()

        return (
            self._chain_factory()
            # .render_if(context.sibling_view, "core/context/sibling.j2", **sibling_ctx)  # Disabled
            .with_user_prompt(context.task_description)
            .with_cve_display(context.cve_instance)
            .render_if(context.sibling_view, "core/context/sibling.j2", **sibling_ctx)
            .render_if(branch == "builder", "secbench/worker/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "secbench/worker/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "secbench/worker/fixer.j2", **cve_ctx)
            .build()
        )

    def build_researcher_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench researcher prompt using layered composition.

        Layer 1: core/roles/researcher.j2 (behavior)
        Layer 2: parent context (global context)
        Layer 3: secbench/context/* (CVE data, constraints)

        RESEARCHER gathers context before MANAGER decomposes, so it needs
        CVE instance data and constraints to guide its research.
        """
        if context.cve_instance is None:
            return None

        cve_ctx = context.cve_instance.to_template_context()
        parent_task = context.spawn_payload.parent_task if context.spawn_payload else None

        return (
            self._chain_factory()
            # Layer 1: Core behavior
            .render("core/roles/researcher.j2")
            # Layer 2: Parent context
            .text_if(parent_task, f"<PARENT_TASK>\n{parent_task}\n</PARENT_TASK>")
            # Layer 3: SEC-bench domain (CVE data + constraints)
            .render("secbench/context/instance.j2", **cve_ctx)
            .render("secbench/context/environment.j2", **cve_ctx)
            .render("secbench/context/constraints.j2", **cve_ctx)
            .build()
        )
