"""SEC-bench prompt strategy implementation."""

from typing import TYPE_CHECKING

from core.application.services import PromptContext
from plugins.security.cve_instance import CVEInstance
from plugins.security.security_tool import get_tools_for_phase


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
    include_decomposition: bool = False,
) -> "TemplateChain":
    """Render the CVE context block.

    ``include_decomposition`` gates cve.j2's subtask-justification guidance: it is
    meaningful only for roles that produce subtasks (boss/manager/assessment).
    Workers and the flat baseline must not carry decomposition vocabulary.
    """
    if cve_instance is None:
        return chain

    cve_ctx = cve_instance.to_template_context()
    cve_display = {k: v for k, v in cve_ctx.items() if k in _CVE_DISPLAY_FIELDS and v}
    return chain.render(
        "domains/secbench/cve.j2",
        cve=cve_display,
        phase=phase,
        include_decomposition_guidance=include_decomposition,
        **cve_ctx,
    )


class SecBenchPromptStrategy:
    """SEC-bench specific prompt strategy."""

    def __init__(
        self,
        enabled_tools: list[str] | None = None,
        shared_code_first: bool = False,
    ) -> None:
        self._enabled_tools = enabled_tools or []
        # When True, the run-global shared code block renders BEFORE the
        # per-phase CVE display and worker mindset, so every branch shares the
        # byte region the block occupies and cross-branch prefix-cache hits
        # become possible. Off keeps the legacy order (cache forks per branch).
        self._shared_code_first = shared_code_first

    def extend_assessment_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None
        cve_ctx = cve_instance.to_template_context()
        chain = _with_cve_display(chain, cve_instance, include_decomposition=True).render_optional(
            "domains/secbench/assess.j2", **cve_ctx
        )
        # Boss recon block in the cached prefix — a PENDING manager does its recon
        # in this assessment phase, so this is where the boss's reads must land to
        # spare re-reads. Empty unless share_boss_recon is on. Manager/leaf-assess
        # only (sonnet); the worker EXECUTION prompt never carries it.
        return chain.text_if(bool(context.shared_code_block), context.shared_code_block)

    def extend_boss_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = cve_instance.to_template_context()
        return _with_cve_display(chain, cve_instance, include_decomposition=True).render(
            "domains/secbench/boss.j2", **cve_ctx
        )

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

        chain = _with_cve_display(
            chain, cve_instance, phase=branch, include_decomposition=True
        ).render("domains/secbench/manager.j2", **cve_ctx)
        # Boss recon block (the files the boss already read), injected into the
        # cached stable prefix. It is fixed once the boss has decomposed, so it
        # is byte-identical across the run's managers and caches cleanly. Empty
        # unless share_boss_recon is on. Manager-only — never reaches workers.
        return chain.text_if(bool(context.shared_code_block), context.shared_code_block)

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

        # A CVE worker with no detectable branch has no phase contract: the
        # shared phase partial (deliverables + verdict gate) is keyed on the
        # branch, so an undetected branch would emit a worker without the very
        # artifact contract that makes arms comparable. Fail loudly rather than
        # silently producing an under-specified prompt.
        if branch is None:
            raise ValueError(
                "SEC-bench worker prompt requested for a CVE task but no phase branch "
                "could be detected from the task description or briefing ancestry. "
                f"task_description={context.task_description!r}. Prefix the task with "
                "[Builder]/[Exploiter]/[Fixer]/[Reporter] or supply an ancestry that "
                "names the phase."
            )

        # Gate the shared-code prompt guidance on the feature actually producing
        # a block. N cells (no provider, flag off) and a run's first worker
        # (nothing observed yet) get the ORIGINAL BEF prompt with no
        # <provided_source_files> mention; only workers that actually receive
        # provided files see the "already in context / don't re-view" guidance.
        cve_ctx["shared_code_enabled"] = bool(context.shared_code_block)

        # Shared code-prefix block placement (pre-rendered by
        # SharedCodeContextProvider — single render site; bytes inserted as-is;
        # empty/None when the shared-context flag is off):
        # - shared_code_first: BEFORE the CVE display and worker mindset. Those
        #   bytes vary per phase (and with shared_code_enabled), so the legacy
        #   order forks the cacheable prefix per branch; block-first keeps the
        #   large payload in the byte region ALL of the run's workers share.
        # - legacy: after CVE display + mindset, before the BEF-role-specific
        #   section — identical-across-workers only within a branch.
        if self._shared_code_first:
            chain = chain.text_if(bool(context.shared_code_block), context.shared_code_block)
        chain = _with_cve_display(chain, cve_instance, phase=branch)
        chain = chain.render("domains/secbench/worker.j2", **cve_ctx)
        if not self._shared_code_first:
            chain = chain.text_if(bool(context.shared_code_block), context.shared_code_block)

        chain = (
            chain.render_if(
                branch == "builder",
                "domains/secbench/worker/builder.j2",
                include_validation_gate=True,
                **cve_ctx,
            )
            .render_if(
                branch == "exploiter",
                "domains/secbench/worker/exploiter.j2",
                include_validation_gate=True,
                **cve_ctx,
            )
            .render_if(
                branch == "fixer",
                "domains/secbench/worker/fixer.j2",
                include_validation_gate=True,
                **cve_ctx,
            )
            .render_if(
                branch == "reporter",
                "domains/secbench/worker/reporter.j2",
                include_validation_gate=True,
                **cve_ctx,
            )
        )
        if self._enabled_tools:
            tools = get_tools_for_phase(branch, self._enabled_tools)
            if tools:
                chain = chain.render(
                    "domains/secbench/tools.j2",
                    security_tools=[tool.model_dump() for tool in tools],
                )

        return chain

    def extend_flat_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        """Compose the SEC-bench flat-mode prompt.

        Renders the CVE problem statement plus ``flat_pipeline.j2``, a thin
        sequential wrapper that includes the same shared phase partials
        (``domains/secbench/phases/{_mindset,build,exploit,fix,report}.j2``)
        the BEF workers receive, so every arm emits byte-identical
        ``/testcase/`` artifacts. The wrapper adds only the single-agent,
        four-phases-in-order framing — no manager, no siblings, no handoff.
        The role/operation/worker templates are intentionally NOT rendered
        here — those encode the tree topology's engineering and would
        contaminate the flat baseline.
        """
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = cve_instance.to_template_context()
        return _with_cve_display(chain, cve_instance, phase=None).render(
            "domains/secbench/flat_pipeline.j2", include_validation_gate=True, **cve_ctx
        )
