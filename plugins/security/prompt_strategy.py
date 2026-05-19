"""SEC-bench prompt strategy implementation."""

from typing import TYPE_CHECKING

from core.application.services import PromptContext
from plugins.security.cve_instance import CVEInstance


if TYPE_CHECKING:
    from core.application.services import TemplateChain
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
)


def detect_benchmark_branch(briefing: "Briefing | None") -> str | None:
    """Detect which SEC-bench branch an agent belongs to.

    Ancestry is root-first, so iterate in reverse (direct parent first)
    to prefer the most specific ancestor over generic BOSS-level tasks.
    """
    if briefing is None:
        return None

    for ancestor in reversed(briefing.ancestry):
        task_lower = ancestor.task_summary.lower()
        if any(kw in task_lower for kw in ("builder", "environment", "setup", "docker pull")):
            return "builder"
        if any(kw in task_lower for kw in ("exploiter", "poc", "exploit", "proof of concept")):
            return "exploiter"
        if any(kw in task_lower for kw in ("fixer", "patch", "fix")):
            return "fixer"
        if any(kw in task_lower for kw in ("reporter", "report", "security report")):
            return "reporter"

    return None


def _detect_branch_from_task(task_description: str) -> str | None:
    task_lower = task_description.lower()
    # Explicit bracket prefixes are authoritative — check these first to
    # avoid false positives from generic words like "poc" or "fix" that
    # can appear in any branch's task description.
    if "[builder]" in task_lower:
        return "builder"
    if "[exploiter]" in task_lower:
        return "exploiter"
    if "[fixer]" in task_lower:
        return "fixer"
    if "[reporter]" in task_lower:
        return "reporter"
    # Fall back to loose keyword matching.
    if any(kw in task_lower for kw in ("builder", "environment", "setup")):
        return "builder"
    if any(kw in task_lower for kw in ("exploiter", "exploit")):
        return "exploiter"
    if any(kw in task_lower for kw in ("fixer", "patch", "fix")):
        return "fixer"
    if any(kw in task_lower for kw in ("reporter", "security report")):
        return "reporter"
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
        cve_ctx = cve_instance.to_template_context()
        return _with_cve_display(chain, cve_instance).render_optional(
            "domains/secbench/assess.j2", **cve_ctx
        )

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

        cve_ctx = cve_instance.to_template_context()
        branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            branch = detect_benchmark_branch(context.briefing)

        chain = _with_cve_display(chain, cve_instance, phase=branch)
        chain = chain.render("domains/secbench/manager.j2", **cve_ctx)

        # OBSOLETE 2026-05-17 — manager/{role}.j2 templates were dead code on
        # the happy path: assess_task → action="decompose" → _apply_assessment_result
        # spawns children directly, bypassing evaluate_task (which is what
        # renders these). They were only reachable via trigger_redecomposition.
        # The .j2 files have been gutted in-place (kept as graveyard files
        # with an explanatory header). See experiments/b1-batch-autogen/
        # FINDINGS.md §5.2. This block stays commented as a marker until the
        # redecomposition path is consciously redesigned.
        # is_direct_boss_child = context.briefing is not None and len(context.briefing.ancestry) == 1
        # if branch is not None and is_direct_boss_child:
        #     chain = (
        #         chain
        #         .render_if(branch == "builder", "domains/secbench/manager/builder.j2", **cve_ctx)
        #         .render_if(branch == "exploiter", "domains/secbench/manager/exploiter.j2", **cve_ctx)
        #         .render_if(branch == "fixer", "domains/secbench/manager/fixer.j2", **cve_ctx)
        #     )

        return chain

    def extend_worker_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = cve_instance.to_template_context()
        branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            branch = detect_benchmark_branch(context.briefing)

        chain = _with_cve_display(chain, cve_instance, phase=branch)
        chain = chain.render("domains/secbench/worker.j2", **cve_ctx)

        if branch is not None:
            chain = (
                chain
                .render_if(branch == "builder", "domains/secbench/worker/builder.j2", **cve_ctx)
                .render_if(branch == "exploiter", "domains/secbench/worker/exploiter.j2", **cve_ctx)
                .render_if(branch == "fixer", "domains/secbench/worker/fixer.j2", **cve_ctx)
                .render_if(branch == "reporter", "domains/secbench/worker/reporter.j2", **cve_ctx)
            )

        return chain

    def extend_flat_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        """Compose the SEC-bench flat-mode prompt.

        Renders the CVE problem statement plus a minimal 4-phase task
        structure (``flat_pipeline.j2``, a subset of ``boss.j2`` with
        decomposition mechanics stripped). The role/operation/worker
        templates are intentionally NOT rendered here — those encode the
        tree topology's engineering and would contaminate the flat
        baseline.
        """
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = cve_instance.to_template_context()
        return _with_cve_display(chain, cve_instance, phase=None).render(
            "domains/secbench/flat_pipeline.j2", **cve_ctx
        )
