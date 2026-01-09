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
    container_id: str | None = None  # SEC-bench container ID for in-container execution
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


class DefaultPromptStrategy:
    """Default strategy - returns None to use base prompts for all roles."""

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        return None


def _detect_benchmark_branch(spawn_payload: "SpawnPayload | None") -> str | None:
    """Detect which SEC-bench branch (build/exploit/fix) this agent belongs to.

    Checks ancestry chain for phase keywords.
    Returns None if not in a benchmark run or branch not determinable.
    """
    if spawn_payload is None:
        return None

    for ancestor in spawn_payload.ancestry:
        task_lower = ancestor.task_summary.lower()
        # Check for Build phase keywords
        if any(kw in task_lower for kw in ("[build]", "build phase", "builder", "environment", "setup")):
            return "builder"
        # Check for Exploit phase keywords
        if any(kw in task_lower for kw in ("[exploit]", "exploit phase", "exploiter", "poc", "proof of concept")):
            return "exploiter"
        # Check for Fix phase keywords
        if any(kw in task_lower for kw in ("[fix]", "fix phase", "fixer", "patch")):
            return "fixer"

    return None


def _detect_branch_from_task(task_description: str) -> str | None:
    """Detect SEC-bench branch from task description keywords."""
    task_lower = task_description.lower()
    # Check for Build phase keywords
    if any(kw in task_lower for kw in ("[build]", "build phase", "[builder]", "builder")):
        return "builder"
    # Check for Exploit phase keywords
    if any(kw in task_lower for kw in ("[exploit]", "exploit phase", "[exploiter]", "exploiter", "poc")):
        return "exploiter"
    # Check for Fix phase keywords
    if any(kw in task_lower for kw in ("[fix]", "fix phase", "[fixer]", "fixer", "patch")):
        return "fixer"
    return None


class SecBenchPromptStrategy:
    """SEC-bench specific prompt strategy with SRP layers.

    Layer 1: Core behavior (core/roles/*.j2) - Agent mechanics
    Layer 2: Shared context (core/context/*.j2) - user_prompt, sibling
    Layer 3: SEC-bench domain (secbench/*.j2) - CVE data, constraints, phases

    Each layer is separate and composable.
    """

    def __init__(self, chain_factory: "Callable[[], TemplateChain]") -> None:
        """Initialize with a factory for creating template chains.

        Args:
            chain_factory: Callable that creates a new TemplateChain instance.
        """
        self._chain_factory = chain_factory

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench boss prompt using layered composition.

        Layer 1: core/roles/boss.j2 (behavior)
        Layer 2: user_prompt (global context)
        Layer 3: secbench/context/* + secbench/boss_decomposition.j2
        """
        if context.cve_instance is None:
            return None

        cve_ctx = context.cve_instance.to_template_context()

        return (
            self._chain_factory()
            # Layer 1: Core behavior
            .render("core/roles/boss.j2", default_tool=context.default_tool)
            # Layer 2: Shared global context
            .with_user_prompt(context.task_description)
            # Layer 3: SEC-bench domain
            .render("secbench/context/instance.j2", **cve_ctx)
            .render("secbench/context/environment.j2", **cve_ctx)
            .render("secbench/context/constraints.j2", **cve_ctx)
            .render("secbench/boss_decomposition.j2")
            .build()
        )

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench manager prompt using layered composition.

        Layer 1: core/roles/manager.j2 (behavior)
        Layer 2: user_prompt + parent context (global context)
        Layer 3: secbench/context/* + secbench/phases/{phase}.j2
        """
        if context.cve_instance is None:
            return None

        branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            branch = _detect_benchmark_branch(context.spawn_payload)
        if branch is None:
            return None

        cve_ctx = context.cve_instance.to_template_context()
        parent_task = context.spawn_payload.parent_task if context.spawn_payload else None

        return (
            self._chain_factory()
            # Layer 1: Core behavior
            .render("core/roles/manager.j2", default_tool=context.default_tool)
            # Layer 2: Shared global context (including parent context)
            .with_user_prompt(context.task_description)
            .text_if(parent_task, f"<PARENT_TASK>\n{parent_task}\n</PARENT_TASK>")
            # Layer 3: SEC-bench domain
            .render("secbench/context/instance.j2", **cve_ctx)
            .render("secbench/context/environment.j2", **cve_ctx)
            .render("secbench/context/constraints.j2", **cve_ctx)
            .render_if(branch == "builder", "secbench/phases/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "secbench/phases/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "secbench/phases/fixer.j2", **cve_ctx)
            # Security ops reference for exploit/fix phases
            .render_if(branch in ("exploiter", "fixer"), "secbench/security_ops.j2")
            .build()
        )

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        """Build SEC-bench worker prompt using layered composition.

        Layer 1: core/roles/worker.j2 (behavior)
        Layer 2: user_prompt + parent context + sibling context (global context)
        Layer 3: secbench/context/* + secbench/phases/{phase}.j2
        """
        if context.cve_instance is None:
            return None

        branch = _detect_benchmark_branch(context.spawn_payload)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            return None

        sibling_ctx = (
            context.sibling_view.to_template_dict()
            if context.sibling_view
            else {}
        )
        cve_ctx = context.cve_instance.to_template_context()
        parent_task = context.spawn_payload.parent_task if context.spawn_payload else None

        if context.container_id:
            cve_ctx["container_id"] = context.container_id

        return (
            self._chain_factory()
            # Layer 1: Core behavior
            .render("core/roles/worker.j2")
            # Layer 2: Shared global context (including parent and sibling context)
            .with_user_prompt(context.task_description)
            .text_if(parent_task, f"<PARENT_TASK>\n{parent_task}\n</PARENT_TASK>")
            .render_if(context.sibling_view, "core/context/sibling.j2", **sibling_ctx)
            # Layer 3: SEC-bench domain
            .render("secbench/context/instance.j2", **cve_ctx)
            .render("secbench/context/environment.j2", **cve_ctx)
            .render("secbench/context/constraints.j2", **cve_ctx)
            .render_if(branch == "builder", "secbench/phases/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "secbench/phases/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "secbench/phases/fixer.j2", **cve_ctx)
            # Security ops reference for exploit/fix phases
            .render_if(branch in ("exploiter", "fixer"), "secbench/security_ops.j2")
            .build()
        )