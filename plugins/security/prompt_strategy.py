"""SEC-bench prompt strategy implementation."""

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from core.application.run_invariants import TaskPromptSpec
from core.application.services import PromptBuilder, PromptContext
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
    "bug_report",
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
    return chain.render("inputs/cve.j2", cve=cve_display, phase=phase, **cve_ctx)


def render_input_block(
    chain: "TemplateChain",
    cve_instance: CVEInstance,
    *,
    phase: str | None = None,
) -> "TemplateChain":
    """Append the canonical input block (user + cve + control) to a chain.

    The same three templates render at every entry point that wants the
    unified input — Cell A's flat-mode worker and Cell B/C/D's BOSS.
    """
    cve_ctx = cve_instance.to_template_context()
    chain = chain.render("inputs/user.j2", **cve_ctx)
    chain = _with_cve_display(chain, cve_instance, phase=phase)
    return chain.render("inputs/control.j2", **cve_ctx)


def render_single_agent_prompt(
    *,
    template_dir: Path,
    cve_instance: CVEInstance,
    task: str,
) -> TaskPromptSpec:
    """Render Cell A's prompt: ``system/single_agent.j2`` + canonical input block.

    Uses the same Jinja machinery and ``render_input_block`` helper that
    the hierarchical BOSS path uses, so the user-message bytes are
    identical between Cell A and Cell B-BOSS for the same CVE — the
    experimental control variable the run matrix relies on.
    """
    builder = PromptBuilder(
        template_dir=template_dir,
        default_tool="claude_code",
        strategy=SecBenchPromptStrategy(),
    )
    cve_ctx = cve_instance.to_template_context()
    chain = builder.chain().render("system/single_agent.j2", **cve_ctx)
    chain = render_input_block(chain, cve_instance)
    rendered = chain.build()
    prompt_sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return TaskPromptSpec(
        rendered_prompt=rendered,
        prompt_sha=prompt_sha,
        cve_context=cve_ctx,
        task=task,
    )


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
        # The input block lands AFTER operations/decomposition.j2 because
        # that's where the strategy hook fires. decomposition.j2 carries
        # its own <task> block, so two <task> tags appear in the
        # rendered BOSS prompt. Harmless to current models but a known
        # cosmetic artifact — fixing it requires moving the strategy
        # call into PromptBuilder.build_boss_delegation_prompt before
        # operations/decomposition.j2 renders. Tracked as follow-up.
        chain = render_input_block(chain, cve_instance)
        return chain.render("domains/secbench/boss.j2", **cve_ctx)

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

        is_direct_boss_child = context.briefing is not None and len(context.briefing.ancestry) == 1
        if branch is not None and is_direct_boss_child:
            chain = (
                chain.render_if(
                    branch == "builder", "domains/secbench/manager/builder.j2", **cve_ctx
                )
                .render_if(
                    branch == "exploiter", "domains/secbench/manager/exploiter.j2", **cve_ctx
                )
                .render_if(branch == "fixer", "domains/secbench/manager/fixer.j2", **cve_ctx)
            )

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
                chain.render_if(
                    branch == "builder", "domains/secbench/worker/builder.j2", **cve_ctx
                )
                .render_if(branch == "exploiter", "domains/secbench/worker/exploiter.j2", **cve_ctx)
                .render_if(branch == "fixer", "domains/secbench/worker/fixer.j2", **cve_ctx)
                .render_if(branch == "reporter", "domains/secbench/worker/reporter.j2", **cve_ctx)
            )

        return chain
