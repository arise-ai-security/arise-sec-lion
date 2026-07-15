"""SEC-bench prompt strategy implementation."""

import re
from typing import TYPE_CHECKING, Final

from core.application.services import PromptContext
from plugins.security.cve_instance import CVEInstance
from plugins.security.deliverables import (
    ARTIFACT_DIRS,
    ARTIFACT_PATHS,
    EXPLOIT_VALIDATION_FIELDS,
    PATCH_VALIDATION_FIELDS,
    PHASE_COMMANDS,
    REQUIRED_FILES,
    REQUIRED_FILES_WITH_PURPOSE,
    ROOT_CAUSE_BLOCK_FIELDS,
    VALIDATION_REQUIRED,
)
from plugins.security.roles import (
    ADAPTIVE_SPECIALISTS,
    COMPACT_ROLES,
    PHASE_ROLES,
    Role,
    deliverables_owned_by_others,
    role_by_name_ci,
)
from plugins.security.security_tool import get_tools_for_phase


if TYPE_CHECKING:
    from core.application.services import TemplateChain


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
    "sanitizer_report",
)

_BRACKET_PREFIX_RE = re.compile(r"^\s*\[([^\]]+)\]")
_PHASE_TO_BRANCH = {
    "builder": "builder",
    "exploiter": "exploiter",
    "fixer": "fixer",
    "reporter": "reporter",
}
_ROLE_TO_BRANCH = {
    role.name.lower(): _PHASE_TO_BRANCH[role.phase.lower()]
    for roles in PHASE_ROLES.values()
    for role in roles
}

_ROLE_TEMPLATE_BY_NAME: Final[dict[str, str]] = {
    "Build-Setup": "domains/secbench/worker/roles/builder/build_setup.j2",
    "Build-Executor": "domains/secbench/worker/roles/builder/build_executor.j2",
    "Build-Verifier": "domains/secbench/worker/roles/builder/build_verifier.j2",
    "PoC-Researcher": "domains/secbench/worker/roles/exploiter/poc_researcher.j2",
    "Data-Flow-Analyst": "domains/secbench/worker/roles/exploiter/data_flow_analyst.j2",
    "PoC-Tester": "domains/secbench/worker/roles/exploiter/poc_tester.j2",
    "Forward-Instrumentator": (
        "domains/secbench/worker/roles/exploiter/forward_instrumentator.j2"
    ),
    "Repro-Creator": "domains/secbench/worker/roles/exploiter/repro_creator.j2",
    "Exploit-Validator": "domains/secbench/worker/roles/exploiter/exploit_validator.j2",
    "Root-Cause-Analyst": "domains/secbench/worker/roles/fixer/root_cause_analyst.j2",
    "Candidate-Reviewer": "domains/secbench/worker/roles/fixer/candidate_reviewer.j2",
    "Regression-Tester": "domains/secbench/worker/roles/fixer/regression_tester.j2",
    "Patch-Applier": "domains/secbench/worker/roles/fixer/patch_applier.j2",
    "Patch-Validator": "domains/secbench/worker/roles/fixer/patch_validator.j2",
    "Fix-Aggregator": "domains/secbench/worker/roles/fixer/fix_aggregator.j2",
    "Reporter": "domains/secbench/worker/roles/reporter/reporter.j2",
}

_PHASE_TEMPLATE_BY_BRANCH: Final[dict[str, str]] = {
    "builder": "domains/secbench/worker/builder.j2",
    "exploiter": "domains/secbench/worker/exploiter.j2",
    "fixer": "domains/secbench/worker/fixer.j2",
    "reporter": "domains/secbench/worker/reporter.j2",
}


def _branch_from_brackets(text: str) -> str | None:
    """Map an explicit ``[Phase]`` or ``[Role]`` prefix to its branch, or None.

    The boss/manager stamps tasks with bracket labels from the role catalog, so
    the prefix is the authoritative phase signal. Loose keyword matching trips
    on tasks that legitimately mention another phase's artifacts.
    """
    match = _BRACKET_PREFIX_RE.match(text)
    if match is None:
        return None
    label = match.group(1).strip().lower()
    return _PHASE_TO_BRANCH.get(label) or _ROLE_TO_BRANCH.get(label)


def detect_benchmark_branch(task_descriptions: tuple[str, ...]) -> str | None:
    """Classify nearest-first ancestor task descriptions into a SEC-bench branch."""
    # Authoritative first: the nearest task stamped with an explicit
    # [Phase] bracket. A Fixer subtask legitimately references the exploit/repro
    # it validates against, so the loose keyword pass below (exploiter before
    # fixer) would otherwise misroute a Fixer leaf to the Exploiter prompt.
    for task_description in task_descriptions:
        bracket = _branch_from_brackets(task_description)
        if bracket is not None:
            return bracket

    for task_description in task_descriptions:
        task_lower = task_description.lower()
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
    # Explicit bracket prefixes are authoritative — check these first to
    # avoid false positives from generic words like "poc" or "fix" that
    # can appear in any branch's task description.
    bracket = _branch_from_brackets(task_description)
    if bracket is not None:
        return bracket
    # Fall back to loose keyword matching.
    task_lower = task_description.lower()
    if any(kw in task_lower for kw in ("builder", "environment", "setup")):
        return "builder"
    if any(kw in task_lower for kw in ("exploiter", "exploit")):
        return "exploiter"
    if any(kw in task_lower for kw in ("fixer", "patch", "fix")):
        return "fixer"
    if any(kw in task_lower for kw in ("reporter", "security report")):
        return "reporter"
    return None


def role_from_task(task_description: str) -> Role | None:
    """Resolve a worker task's explicit ``[Role]`` bracket to its catalog role.

    A leaf worker carries its own role label, so the role is read from the task
    description directly. Phase brackets (``[Exploiter]``) and out-of-catalog
    labels resolve to None. Worker rendering distinguishes those cases and
    rejects unknown bracket labels instead of granting a whole-phase contract.
    """
    match = _BRACKET_PREFIX_RE.match(task_description)
    if match is None:
        return None
    return role_by_name_ci(match.group(1).strip())


def _leading_bracket_label(task_description: str) -> str | None:
    match = _BRACKET_PREFIX_RE.match(task_description)
    return match.group(1).strip() if match is not None else None


def _as_cve_instance(domain_context: object | None) -> CVEInstance | None:
    return domain_context if isinstance(domain_context, CVEInstance) else None


def _with_contract_context(cve_ctx: dict[str, object]) -> dict[str, object]:
    cve_ctx["artifact_dirs"] = ARTIFACT_DIRS
    cve_ctx["artifact_paths"] = ARTIFACT_PATHS
    cve_ctx["phase_commands"] = PHASE_COMMANDS
    cve_ctx["required_files"] = REQUIRED_FILES
    cve_ctx["required_files_with_purpose"] = REQUIRED_FILES_WITH_PURPOSE
    cve_ctx["validation_required"] = VALIDATION_REQUIRED
    cve_ctx["root_cause_block_fields"] = ROOT_CAUSE_BLOCK_FIELDS
    cve_ctx["exploit_validation_fields"] = EXPLOIT_VALIDATION_FIELDS
    cve_ctx["patch_validation_fields"] = PATCH_VALIDATION_FIELDS
    return cve_ctx


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

    cve_ctx = _with_contract_context(cve_instance.to_template_context())
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
        cve_ctx = _with_contract_context(cve_instance.to_template_context())
        # Inject the role catalog (single source of truth) so assess.j2 renders the
        # decomposition role menu from plugins/security/roles.py instead of hardcoding.
        cve_ctx["phase_roles"] = PHASE_ROLES
        cve_ctx["all_roles"] = [role for roles in PHASE_ROLES.values() for role in roles]
        # Gate the "read source before decomposing" recon block: when the boss already
        # shared its recon (the block injected below), tell the manager to build on it
        # instead of re-reading — mirrors the worker prompts' shared_code_enabled flag.
        cve_ctx["shared_code_enabled"] = bool(context.shared_code_block)
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

        cve_ctx = _with_contract_context(cve_instance.to_template_context())
        cve_ctx["phase_roles"] = PHASE_ROLES
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

        cve_ctx = _with_contract_context(cve_instance.to_template_context())
        # Render every phase catalog in the stable prefix. The current phase and
        # failure history live after the cache breakpoint, so neither may select
        # content here without destroying cross-branch/retry prefix reuse.
        cve_ctx["phase_catalogs"] = tuple(
            {
                "phase": phase,
                "compact_roles": tuple(
                    role
                    for name in COMPACT_ROLES.get(phase, ())
                    if (role := role_by_name_ci(name)) is not None
                ),
                "adaptive_specialists": tuple(
                    role
                    for name in ADAPTIVE_SPECIALISTS.get(phase, ())
                    if (role := role_by_name_ci(name)) is not None
                ),
            }
            for phase in PHASE_ROLES
        )

        chain = _with_cve_display(
            chain, cve_instance, include_decomposition=True
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

        cve_ctx = _with_contract_context(cve_instance.to_template_context())
        label = _leading_bracket_label(context.task_description)
        worker_role = role_from_task(context.task_description)
        if label is not None and worker_role is None and label.lower() not in _PHASE_TO_BRANCH:
            raise ValueError(
                f"Unknown SEC-bench worker role label [{label}]. Use a canonical phase "
                "label or one of the catalog roles."
            )

        branch = _detect_branch_from_task(context.task_description)
        if branch is None:
            branch = detect_benchmark_branch(context.ancestor_task_descriptions)

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

        # Role-ownership gate: a leaf worker stamped with a catalog role (e.g.
        # [Repro-Creator]) gets a body scoped to ITS deliverable; the whole-phase
        # body (and the sibling roles' deliverable instructions) are gated out so
        # the role layer the manager created is not erased at the worker tier. A
        # phase-level task ([Exploiter]) resolves to no role and keeps the full
        # whole-phase runbook.
        cve_ctx["worker_role"] = worker_role
        cve_ctx["foreign_deliverables"] = (
            deliverables_owned_by_others(worker_role) if worker_role is not None else ()
        )

        # Gate the shared-code prompt guidance on the feature actually producing
        # a block. Runs with no provider/flag and a run's first worker
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
        # Static runtime contract first — cross-CVE/cross-run identical, so it
        # sits in the shared cache prefix ahead of the per-CVE cve.j2 block.
        chain = chain.render("domains/secbench/_secb_runtime_contract.j2", **cve_ctx)
        if self._shared_code_first:
            chain = chain.text_if(bool(context.shared_code_block), context.shared_code_block)
        chain = _with_cve_display(chain, cve_instance, phase=branch)
        chain = chain.render("domains/secbench/worker.j2", **cve_ctx)
        if not self._shared_code_first:
            chain = chain.text_if(bool(context.shared_code_block), context.shared_code_block)

        if worker_role is not None:
            chain = chain.render(
                "domains/secbench/worker/role_contract.j2",
                **cve_ctx,
            ).render(
                _ROLE_TEMPLATE_BY_NAME[worker_role.name],
                **cve_ctx,
            )
        else:
            chain = chain.render(
                _PHASE_TEMPLATE_BY_BRANCH[branch],
                include_validation_gate=True,
                **cve_ctx,
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
        used by whole-phase worker fallbacks. Catalog roles derive their owned
        outputs from the same deliverable constants, so every arm emits the same
        ``/testcase/`` contract. The wrapper adds only the single-agent,
        four-phases-in-order framing — no manager, no siblings, no handoff.
        The role/operation/worker templates are intentionally NOT rendered
        here — those encode the tree topology's engineering and would
        contaminate the flat baseline.
        """
        cve_instance = _as_cve_instance(context.domain_context)
        if cve_instance is None:
            return None

        cve_ctx = _with_contract_context(cve_instance.to_template_context())

        # Static runtime contract first (see extend_worker_prompt) for cache prefix.
        chain = chain.render("domains/secbench/_secb_runtime_contract.j2", **cve_ctx)
        return _with_cve_display(chain, cve_instance, phase=None).render(
            "domains/secbench/flat_pipeline.j2", include_validation_gate=True, **cve_ctx
        )
