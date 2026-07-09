"""Tests for the SEC-bench decomposition role-contract validator.

The validator classifies a phase manager's proposed child decomposition against the
role catalog: every REQUIRED role must appear as its own single-role leaf. Missing
roles become injection suggestions; leaves merging >1 catalog role are flagged for
removal and split.
"""

from plugins.security.cve_instance import CVEInstance
from plugins.security.decomposition_validator import SecBenchDecompositionValidator


EXPLOITER_REQUIRED = (
    "PoC-Researcher",
    "Data-Flow-Analyst",
    "PoC-Tester",
    "Forward-Instrumentator",
    "Repro-Creator",
    "Exploit-Validator",
)


def _cve() -> CVEInstance:
    return CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="demo/p",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="Heap overflow.",
        base_commit="a" * 40,
    )


def test_complete_exploiter_decomposition_is_ok() -> None:
    v = SecBenchDecompositionValidator()
    subs = tuple(f"[{r}] do {r}" for r in EXPLOITER_REQUIRED)
    verdict = v.classify(
        parent_task_description="[Exploiter] Reproduce the vulnerability",
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions
    assert not verdict.removals


def test_missing_exploit_validator_is_injected() -> None:
    v = SecBenchDecompositionValidator()
    subs = tuple(f"[{r}] do {r}" for r in EXPLOITER_REQUIRED if r != "Exploit-Validator")
    verdict = v.classify(
        parent_task_description="[Exploiter] Reproduce the vulnerability",
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert not verdict.removals
    assert any(a.description.startswith("[Exploit-Validator]") for a in verdict.additions)
    assert any(viol.kind == "missing_required_role" for viol in verdict.violations)


def test_merged_repro_validator_leaf_is_split() -> None:
    v = SecBenchDecompositionValidator()
    # 4 single-role leaves + 1 merged leaf carrying two Exploiter roles (the libxml2 case).
    subs = (
        "[PoC-Researcher] map",
        "[Data-Flow-Analyst] trace",
        "[PoC-Tester] run",
        "[Forward-Instrumentator] probe",
        "[Repro-Creator] write repro.sh and [Exploit-Validator] validate and write verdict",
    )
    verdict = v.classify(
        parent_task_description="[Exploiter] Reproduce the vulnerability",
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert 4 in verdict.removals  # the merged leaf is dropped
    labels = [a.description.split("]")[0] + "]" for a in verdict.additions]
    assert "[Repro-Creator]" in labels
    assert "[Exploit-Validator]" in labels
    assert any(viol.kind == "merged_leaf" for viol in verdict.violations)


def test_non_cve_domain_context_is_noop() -> None:
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="[Exploiter] x",
        subtask_descriptions=("[PoC-Researcher] x",),
        domain_context=None,
    )
    assert verdict.ok


def test_non_phase_parent_is_noop() -> None:
    # A boss-level task carries no phase bracket → no leaf-role contract to enforce.
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="Reproduce and fix the CVE",
        subtask_descriptions=("[Builder] x",),
        domain_context=_cve(),
    )
    assert verdict.ok


def test_phase_resolved_only_from_leading_bracket() -> None:
    # An incidental phase name mid-text must NOT trigger the phase contract — only a
    # leading [Phase] bracket counts.
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="Investigate the [Exploiter] phase artifacts",
        subtask_descriptions=("[PoC-Researcher] x",),
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions


def test_role_bracket_parent_is_noop() -> None:
    # A leaf-role-bracketed parent (a worker that itself decomposed) does NOT own the
    # phase's full role set, so it must NOT be expanded into the phase contract.
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="[Repro-Creator] write the repro script",
        subtask_descriptions=("[PoC-Tester] run the poc",),
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions


def test_injected_leaf_carries_insight_and_validator_exposes_hard_deps() -> None:
    # An injected leaf must carry the role's empirical insight (not just its summary), and
    # the validator must expose catalog hard deps (core resolves them to sibling indices).
    v = SecBenchDecompositionValidator()
    subs = tuple(f"[{r}] do {r}" for r in EXPLOITER_REQUIRED if r != "Exploit-Validator")
    verdict = v.classify(
        parent_task_description="[Exploiter] Reproduce the vulnerability",
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    ev = next(a for a in verdict.additions if a.description.startswith("[Exploit-Validator]"))
    assert "deterministic" in ev.description.lower()  # role.insight carried, not just summary
    assert v.hard_dependencies("Exploit-Validator") == ("Repro-Creator",)
    assert v.hard_dependencies("PoC-Researcher") == ()  # role with no hard producer


def test_builder_phase_required_roles_enforced() -> None:
    # Generic across phases: a Builder decomposition missing Build-Verifier is repaired.
    v = SecBenchDecompositionValidator()
    subs = ("[Build-Setup] x", "[Build-Compiler] x")
    verdict = v.classify(
        parent_task_description="[Builder] Build the project",
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert any(a.description.startswith("[Build-Verifier]") for a in verdict.additions)
