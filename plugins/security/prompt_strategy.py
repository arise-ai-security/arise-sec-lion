"""SEC-bench prompt strategy implementation."""

from collections.abc import Callable
from typing import TYPE_CHECKING

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
        """Return a direct-execute prompt for known SEC-bench phases.

        SEC-bench tasks follow a fixed 3-phase pipeline (Builder → Exploiter
        → Fixer). The BOSS already decomposes into these well-scoped phases,
        so PENDING children should execute directly as WORKERs — no further
        decomposition is useful.

        Without this override, the generic assess.j2 prompt runs a full
        recon tool-calling loop (read_file, search_codebase, etc.) over the
        entire codebase.  This causes two problems:

        1. **Context window overflow** — recon accumulates tool results
           across iterations, easily exceeding gpt-4o-mini's 128K limit
           (observed: ~1M tokens).
        2. **Over-decomposition** — exploring the codebase makes the LLM
           perceive more complexity, so it almost always chooses "decompose",
           cascading until hitting the max_total_agents cap.

        By returning a direct prompt for known phases, we skip recon entirely
        and the LLM simply returns {"action": "execute"}.
        """
        branch = detect_benchmark_branch(context.briefing)
        if branch is None:
            branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            # Unknown phase — fall back to generic assessment with recon.
            return None

        # Known SEC-bench phase: instruct the LLM to execute directly.
        # The prompt is intentionally minimal — no recon tool instructions,
        # no codebase exploration.  The LLM receives the task description
        # and responds with a simple execute action.
        return (
            self._chain_factory()
            .render("system.j2", default_tool=context.default_tool)
            .render("roles/pending.j2")
            .text(
                f"<task>\n{context.task_description}\n</task>\n\n"
                "<operation>\n"
                f"This is a SEC-bench [{branch}] phase task.  "
                "These phases are already well-scoped leaf tasks produced by "
                "the BOSS decomposition — execute directly as a WORKER.\n\n"
                "Do NOT decompose further.  Do NOT use reconnaissance tools.\n\n"
                "Return ONLY valid JSON:\n"
                '```json\n{"action": "execute", "reasoning": "SEC-bench '
                f'{branch} phase — well-scoped leaf task, execute directly'
                '"}\n```\n'
                "</operation>"
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
