"""Single source of truth for SEC-bench decomposition roles (the BEF leaf workers).

A *role* is a bracket label a phase manager decomposes into, e.g. ``[PoC-Researcher]``.
This is cybersecurity domain data (the experiment's decomposition contract), not
topology or orchestration: it only *describes* roles and their artifact dependencies.

``deliverables.py`` paths are mirrored in each role's ``produces`` so the eval
layer can check exactly the roles that actually ran.

Adaptive selection (the manager picks the roles a CVE needs) is described by two fields:
``depends_on`` (hard producer roles whose artifacts this role consumes) and
``soft_depends_on`` (optional inputs the role uses if present and otherwise
regenerates/degrades — never a blind reference). The system does not repair missing
dependencies; prompt noncompliance is reported by evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from plugins.security.deliverables import (
    ARTIFACT_PATHS,
    REQUIRED_FILES,
    ROOT_CAUSE_BLOCK_KEYS,
    VALIDATION_REQUIRED,
)


# Phase keys match deliverables.REQUIRED_FILES and BefPhase.value in the eval layer.
PHASE_ORDER: Final[tuple[str, ...]] = ("Builder", "Exploiter", "Fixer", "Reporter")


@dataclass(frozen=True)
class Role:
    """One decomposition role and its artifact contract.

    ``produces``      — the MANDATED deliverable paths this role owns; must mirror
                        deliverables.py. Evidence-only outputs (e.g. poc_operation_map.txt,
                        forward_instrumentation.log) belong in ``insight``, NOT here.
    ``depends_on``    — upstream *roles* whose outputs this role hard-requires.
    ``soft_depends_on`` — upstream roles used if present, otherwise the role must
                          regenerate/degrade (the input is not guaranteed to exist).
    ``required``      — part of the phase's core chain (always spawned) vs optional
                        (selected only when the CVE needs it).
    ``insight``       — the empirical question seed for security_insights; the full
                        CWE-conditional guidance stays in assess.j2.
    """

    name: str
    phase: str
    required: bool
    summary: str
    produces: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    soft_depends_on: tuple[str, ...] = ()
    insight: str = ""


# ---------------------------------------------------------------------------
# The catalog. Order within a phase is the intended execution order.
# Builder has three catalog roles. Historical runs that used the old
# "Instrumented-Builder" label are handled as an evaluation alias.
# ---------------------------------------------------------------------------
ROLES: Final[tuple[Role, ...]] = (
    # -- Builder (infrastructure; tight required chain) ---------------------
    Role(
        name="Build-Setup",
        phase="Builder",
        required=True,
        summary="Identify the vulnerable base commit and install build dependencies.",
        produces=(ARTIFACT_PATHS["base_commit_hash"],),
        insight="Which exact commit is the vulnerable base for the SEC-bench build?",
    ),
    Role(
        name="Build-Compiler",
        phase="Builder",
        required=True,
        summary="Run secb build, repair build inputs when needed, and declare binaries.",
        produces=(
            ARTIFACT_PATHS["build_script"],
            ARTIFACT_PATHS["repo_changes_diff"],
            ARTIFACT_PATHS["binary_paths"],
        ),
        depends_on=("Build-Setup",),
        insight="Does secb build produce downstream-callable sanitizer binary path(s)?",
    ),
    Role(
        name="Build-Verifier",
        phase="Builder",
        required=True,
        summary="Verify the instrumented binary is real, non-vacuous, and runs under ASan.",
        depends_on=("Build-Compiler",),
        insight="Does the produced binary run the PoC path without missing-ASan-runtime errors?",
    ),
    # -- Exploiter ----------------------------------------------------------
    Role(
        name="PoC-Researcher",
        phase="Exploiter",
        required=False,
        summary="Map each PoC operation to its implementing C/C++ source function.",
        insight=(
            "Map each PoC operation to its implementing source function via search_codebase; "
            "save the ordered mapping (required input for the Forward-Instrumentator)."
        ),
    ),
    Role(
        name="Data-Flow-Analyst",
        phase="Exploiter",
        required=False,
        summary="Trace the call graph / data flow to the vulnerable path (cflow, xrefs).",
        insight="Which call chain carries attacker-controlled data to the crash site?",
    ),
    Role(
        name="PoC-Tester",
        phase="Exploiter",
        required=False,
        summary="Confirm the PoC empirically triggers the reported sanitizer error.",
        soft_depends_on=("PoC-Researcher",),
        insight="Does running the PoC reproduce the exact sanitizer error type and top frame?",
    ),
    Role(
        name="Forward-Instrumentator",
        phase="Exploiter",
        required=False,
        summary="Forward-trace PoC operations to find where bad internal state is first produced.",
        depends_on=("PoC-Researcher",),
        insight=(
            "For each PoC operation BEFORE the crash, inject fprintf probes into its implementing "
            "function to dump struct fields; the first internally-inconsistent one is the origin. "
            "This is FORWARD tracing, not crash-site forensics."
        ),
    ),
    Role(
        name="Repro-Creator",
        phase="Exploiter",
        required=True,
        summary="Select the PoC and write the secb repro script for the Builder binary.",
        produces=(ARTIFACT_PATHS["poc_path"], ARTIFACT_PATHS["repro_script"]),
        soft_depends_on=("PoC-Researcher", "PoC-Tester"),
        insight="Does secb repro use the selected PoC and Builder binary, crashing 3/3?",
    ),
    Role(
        name="Exploit-Validator",
        phase="Exploiter",
        required=True,
        summary="Validate the reproducer is deterministic, matches the CVE; write verdict file.",
        produces=(ARTIFACT_PATHS["exploit_validation"],),
        depends_on=("Repro-Creator",),
        insight="Do both sanitizer errors and crash functions agree across 3/3 deterministic runs?",
    ),
    # -- Fixer --------------------------------------------------------------
    Role(
        name="Root-Cause-Analyst",
        phase="Fixer",
        required=True,
        summary="Root-cause the bug at code-block level; emit the structured fix-site block.",
        produces=(ARTIFACT_PATHS["root_cause_analysis"],),
        soft_depends_on=("Forward-Instrumentator",),
        insight=(
            "Identify the data-corruption origin (not just the crash site) with live "
            "instrumentation, and emit the block keys: " + ", ".join(ROOT_CAUSE_BLOCK_KEYS)
        ),
    ),
    Role(
        name="Candidate-Reviewer",
        phase="Fixer",
        required=False,
        summary="Evaluate >=2 candidate fix layers against contract/coverage/false-positive risk.",
        depends_on=("Root-Cause-Analyst",),
        insight="Which fix layer extends an existing contract without firing on valid inputs?",
    ),
    Role(
        name="Regression-Tester",
        phase="Fixer",
        required=False,
        summary="Regression-test fix candidates against the repro and the existing suite.",
        soft_depends_on=("Candidate-Reviewer",),
        insight="Does the candidate fix the crash without breaking unrelated behavior?",
    ),
    Role(
        name="Patch-Creator",
        phase="Fixer",
        required=True,
        summary="Generate the patch from source edits at the analyst fix site (not hand-written).",
        produces=(ARTIFACT_PATHS["model_patch"],),
        depends_on=("Root-Cause-Analyst",),
        insight="Does the diff touch only PROPOSED_FIX_SITE (or justify), one logical hunk?",
    ),
    Role(
        name="Patch-Validator",
        phase="Fixer",
        required=True,
        summary="Apply, rebuild, and re-run the repro to confirm the fix; write the verdict file.",
        produces=(ARTIFACT_PATHS["patch_validation"],),
        depends_on=("Patch-Creator",),
        insight="Does the patch apply clean, build, and yield 3/3 no-crash on the repro?",
    ),
    Role(
        name="Fix-Aggregator",
        phase="Fixer",
        required=False,
        summary="Aggregate the validated fix into a concise fix narrative.",
        produces=(ARTIFACT_PATHS["fix_summary"],),
        depends_on=("Patch-Validator",),
        insight="What is the minimal, validated story from root cause to verified fix?",
    ),
    # -- Reporter -----------------------------------------------------------
    Role(
        name="Reporter",
        phase="Reporter",
        required=True,
        summary="Synthesize Builder/Exploiter/Fixer findings into the security report.",
        produces=(ARTIFACT_PATHS["security_report"],),
        soft_depends_on=("Patch-Validator", "Exploit-Validator", "Build-Verifier"),
        insight="Is every claim backed by a tool output, conflicts resolved toward the evidence?",
    ),
)


_BY_NAME: Final[dict[str, Role]] = {r.name: r for r in ROLES}

PHASE_ROLES: Final[dict[str, tuple[Role, ...]]] = {
    phase: tuple(r for r in ROLES if r.phase == phase) for phase in PHASE_ORDER
}


def role_by_name(name: str) -> Role | None:
    """The role for a bracket label (without brackets), or None if unknown."""
    return _BY_NAME.get(name)


def decomposition_only_deliverables(phase: str) -> tuple[str, ...]:
    """Deliverables a phase's roles produce that are NOT in the flat phase contract.

    These are mandated only when a required phase role produces them — the
    ``HIERARCHICAL_ONLY`` set in deliverables.py, derived here from the catalog so
    the role layer stays authoritative. Order follows the role execution order.
    Optional role outputs remain useful evidence but are not key-file failures.
    """
    flat = set(REQUIRED_FILES.get(phase, ())) | set(VALIDATION_REQUIRED.get(phase, ()))
    out: list[str] = []
    for role in PHASE_ROLES.get(phase, ()):
        if not role.required:
            continue
        out.extend(p for p in role.produces if p not in flat and p not in out)
    return tuple(out)
