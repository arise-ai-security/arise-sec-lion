"""SEC-bench prompt strategy implementation."""

from typing import TYPE_CHECKING

from core.application.services.prompt_strategy import PromptContext
from plugins.security.cve_instance import CVEInstance


if TYPE_CHECKING:
    from core.application.services.prompt_builder import TemplateChain
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

    def extend_assessment_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None
        return _with_cve_display(chain, cve_instance)

    def extend_boss_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = cve_instance.to_template_context()
        return _with_cve_display(chain, cve_instance).render("domains/secbench/boss.j2", **cve_ctx)

    def extend_manager_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
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
            _with_cve_display(chain, cve_instance, phase=branch)
            .render_if(branch == "builder", "domains/secbench/manager/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "domains/secbench/manager/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "domains/secbench/manager/fixer.j2", **cve_ctx)
        )

    def extend_worker_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        branch = detect_benchmark_branch(context.briefing)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            return None

        cve_ctx = cve_instance.to_template_context()

        return (
            _with_cve_display(chain, cve_instance, phase=branch)
            .render_if(branch == "builder", "domains/secbench/worker/builder.j2", **cve_ctx)
            .render_if(branch == "exploiter", "domains/secbench/worker/exploiter.j2", **cve_ctx)
            .render_if(branch == "fixer", "domains/secbench/worker/fixer.j2", **cve_ctx)
        )
