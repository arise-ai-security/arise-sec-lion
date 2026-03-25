"""SEC-bench prompt strategy implementation."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.application.services.prompt_builder import compute_limits_context
from core.application.services.prompt_strategy import PromptContext
from plugins.security.cve_instance import CVEInstance

if TYPE_CHECKING:
    from core.application.services.prompt_builder import TemplateChain
    from core.application.services.prompt_strategy import PromptStrategy
    from core.domain.values.node_message import Briefing


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


def detect_benchmark_branch(briefing: "Briefing | None") -> str | None:
    """Detect which SEC-bench branch an agent belongs to."""
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
    task_lower = task_description.lower()
    if any(kw in task_lower for kw in ("[builder]", "builder", "environment", "setup")):
        return "builder"
    if any(kw in task_lower for kw in ("[exploiter]", "exploiter", "poc", "exploit")):
        return "exploiter"
    if any(kw in task_lower for kw in ("[fixer]", "fixer", "patch", "fix")):
        return "fixer"
    return None


def _as_cve_instance(domain_context: object | None) -> CVEInstance | None:
    return domain_context if isinstance(domain_context, CVEInstance) else None


def _with_cve_display(
    chain: "TemplateChain",
    cve_instance: CVEInstance | None,
    *,
    phase: str | None = None,
) -> "TemplateChain":
    if cve_instance is None:
        return chain

    cve_ctx = cve_instance.to_template_context()
    cve_display = {k: v for k, v in cve_ctx.items() if k in _CVE_DISPLAY_FIELDS and v}
    return chain.render("domains/secbench/cve.j2", cve=cve_display, phase=phase, **cve_ctx)


class SecBenchPromptStrategy:
    """SEC-bench specific prompt strategy."""

    def __init__(self, chain_factory: "Callable[[], TemplateChain]") -> None:
        self._chain_factory = chain_factory

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = cve_instance.to_template_context()
        return (
            _with_cve_display(
                self._chain_factory()
                .render("system.j2", default_tool=context.default_tool)
                .render("roles/boss.j2")
                .text(f"<user_prompt>\n{context.task_description}\n</user_prompt>"),
                cve_instance,
            )
            .render("domains/secbench/boss.j2", **cve_ctx, default_tool=context.default_tool)
            .build()
        )

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        branch = detect_benchmark_branch(context.briefing)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            return None

        is_direct_boss_child = context.briefing is not None and len(context.briefing.ancestry) == 1
        if not is_direct_boss_child:
            return None

        cve_ctx = cve_instance.to_template_context()
        return (
            _with_cve_display(
                self._chain_factory()
                .render("system.j2", default_tool=context.default_tool)
                .render("roles/manager.j2")
                .text(f"<user_prompt>\n{context.task_description}\n</user_prompt>"),
                cve_instance,
                phase=branch,
            )
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
        """Return a phase-aware assessment prompt for SEC-bench tasks.

        The LLM can choose execute OR decompose based on phase complexity,
        guided by the domain template.  Recon tools are skipped — the
        domain context provides enough information to decide.

        Depth-awareness via ``ancestry_depth``:
        - depth 1 (direct BOSS child): decomposition is valuable for
          complex phases — the manager templates define rich sub-tasks.
        - depth 2+ (sub-task from a manager template): already pre-scoped,
          strongly defaults to execute.

        Structural safeguards (``at_max_depth``, ``agents_remaining``) are
        enforced via the shared ``assess_output.j2`` template.
        """
        branch = detect_benchmark_branch(context.briefing)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            return None

        cve_instance = _as_cve_instance(context.domain_context)
        cve_id = cve_instance.cve_id if cve_instance else "unknown"
        ancestry_depth = len(context.briefing.ancestry) if context.briefing else 0
        limits = compute_limits_context(context.hierarchy_limits)

        return (
            _with_cve_display(
                self._chain_factory()
                .render("system.j2", default_tool=context.default_tool)
                .render("roles/pending.j2"),
                cve_instance,
                phase=branch,
            )
            .render(
                "domains/secbench/assess.j2",
                phase=branch,
                cve_id=cve_id,
                task_description=context.task_description,
                ancestry_depth=ancestry_depth,
                briefing=context.briefing,
                **limits,
            )
            .render(
                "operations/assess_output.j2",
                default_tool=context.default_tool,
                **limits,
            )
            .build()
        )

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        branch = detect_benchmark_branch(context.briefing)
        if branch is None:
            return None

        sibling_ctx = context.handoff.to_template_dict() if context.handoff else {}
        cve_ctx = cve_instance.to_template_context()

        return (
            _with_cve_display(
                self._chain_factory()
                .render("system.j2", default_tool=context.default_tool)
                .render("roles/worker.j2")
                .text(f"<user_prompt>\n{context.task_description}\n</user_prompt>"),
                cve_instance,
                phase=branch,
            )
            .render_if(context.handoff, "context/sibling.j2", **sibling_ctx)
            .render_if(branch == "builder", "domains/secbench/worker/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "domains/secbench/worker/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "domains/secbench/worker/fixer.j2", **cve_ctx)
            .build()
        )
