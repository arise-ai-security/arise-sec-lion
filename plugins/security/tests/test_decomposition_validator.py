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
    subs = ("[Build-Setup] x", "[Build-Executor] x")
    verdict = v.classify(
        parent_task_description="[Builder] Build the project",
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert any(a.description.startswith("[Build-Verifier]") for a in verdict.additions)


def test_adaptive_policy_returns_fixed_skeleton_then_compact_route() -> None:
    # Given: Adaptive B4 policy and a SEC-bench task
    validator = SecBenchDecompositionValidator(adaptive_execution=True)

    # When: The root and Exploiter controller request fixed decompositions
    skeleton = validator.fixed_decomposition(
        parent_task_description="Repair the CVE",
        domain_context=_cve(),
        redecomposition_count=0,
    )
    compact = validator.fixed_decomposition(
        parent_task_description="[Exploiter] reproduce",
        domain_context=_cve(),
        redecomposition_count=0,
    )

    # Then: The root skeleton is fixed and Exploiter starts compact
    assert skeleton is not None
    assert [item.description.split("]")[0] for item in skeleton.subtasks] == [
        "[Builder",
        "[Exploiter",
        "[Fixer",
        "[Reporter",
    ]
    assert compact is not None
    assert compact.route == "compact"
    assert [item.description.split("]")[0] for item in compact.subtasks] == [
        "[Repro-Creator",
        "[Exploit-Validator",
    ]


def test_adaptive_failed_compact_route_expands_only_same_phase() -> None:
    # Given: An Exploiter controller re-entering after its phase gate failed
    validator = SecBenchDecompositionValidator(adaptive_execution=True)

    # When: The route is selected for redecomposition
    selection = validator.fixed_decomposition(
        parent_task_description="[Exploiter] reproduce",
        domain_context=_cve(),
        redecomposition_count=1,
    )

    # Then: Only exploit specialists are added and the route is escalated
    assert selection is not None
    assert selection.route == "escalated"
    labels = {item.description.split("]")[0] for item in selection.subtasks}
    assert labels == {
        "[PoC-Researcher",
        "[PoC-Tester",
        "[Repro-Creator",
        "[Exploit-Validator",
    }


def test_policy_version_is_sourced_from_config_not_hardcoded() -> None:
    # Given: a validator configured with a specific treatment/policy version
    validator = SecBenchDecompositionValidator(
        adaptive_execution=True, policy_version="b4-adaptive-v2"
    )

    # When: it produces both the skeleton and a phase route
    skeleton = validator.fixed_decomposition(
        parent_task_description="Repair the CVE",
        domain_context=_cve(),
        redecomposition_count=0,
    )
    compact = validator.fixed_decomposition(
        parent_task_description="[Exploiter] reproduce",
        domain_context=_cve(),
        redecomposition_count=0,
    )

    # Then: the emitted policy version reflects config, not a hardcoded literal
    assert skeleton is not None and skeleton.policy_version == "b4-adaptive-v2"
    assert compact is not None and compact.policy_version == "b4-adaptive-v2"


def test_role_fused_policy_spawns_phase_workers_without_role_decomposition() -> None:
    # Given: The adaptive role-fused treatment
    validator = SecBenchDecompositionValidator(
        adaptive_execution=True,
        policy_version="b4-adaptive-rolefused-v1",
    )

    # When: Root and phase tasks request policy-controlled decomposition
    skeleton = validator.fixed_decomposition(
        parent_task_description="Repair the CVE",
        domain_context=_cve(),
        redecomposition_count=0,
    )
    phase = validator.fixed_decomposition(
        parent_task_description="[Fixer] repair the vulnerability",
        domain_context=_cve(),
        redecomposition_count=0,
    )

    # Then: Root phases are atomic workers and never expand into compact role leaves
    assert skeleton is not None
    assert all(item.estimated_complexity == "simple" for item in skeleton.subtasks)
    assert phase is None


def test_exploiter_expansion_is_conditioned_on_the_failure_signal() -> None:
    # Given: an Exploiter controller re-entering after a reproduction/data-flow conflict
    validator = SecBenchDecompositionValidator(adaptive_execution=True)

    # When: the escalation is selected with that specific failure signal
    selection = validator.fixed_decomposition(
        parent_task_description="[Exploiter] reproduce",
        domain_context=_cve(),
        redecomposition_count=1,
        failure_signal="reproduction conflicts with the CVE evidence; data flow unclear",
    )

    # Then: the compact roles are preserved and only the data-flow specialists are added
    assert selection is not None and selection.route == "escalated"
    labels = {item.description.split("]")[0] for item in selection.subtasks}
    assert "[Repro-Creator" in labels and "[Exploit-Validator" in labels  # compact preserved
    assert "[Data-Flow-Analyst" in labels and "[Forward-Instrumentator" in labels
    # the sanitizer-trigger specialists are NOT pulled in for this signal
    assert "[PoC-Tester" not in labels


def test_fixer_patch_failure_expands_with_regression_tester() -> None:
    # Given: a Fixer controller re-entering after patch validation regressed
    validator = SecBenchDecompositionValidator(adaptive_execution=True)

    # When: the escalation is selected with a patch/regression failure signal
    selection = validator.fixed_decomposition(
        parent_task_description="[Fixer] fix",
        domain_context=_cve(),
        redecomposition_count=1,
        failure_signal="patch validation failed: the repro still crashes after rebuild",
    )

    # Then: the targeted Regression-Tester is added, compact Fixer roles preserved,
    # and the whole catalog is NOT injected (stays same-phase)
    assert selection is not None and selection.route == "escalated"
    labels = {item.description.split("]")[0] for item in selection.subtasks}
    assert "[Regression-Tester" in labels
    assert "[Root-Cause-Analyst" in labels and "[Patch-Applier" in labels
    assert "[PoC-Researcher" not in labels  # no cross-phase leakage


def test_invalid_or_incomplete_route_falls_back_to_expanded() -> None:
    # Given/When: Route policy receives invalid or incomplete decisions
    invalid = SecBenchDecompositionValidator.safe_route("unknown", decision_complete=True)
    incomplete = SecBenchDecompositionValidator.safe_route(
        "compact", decision_complete=False
    )

    # Then: Both fail safe to expanded
    assert invalid == "expanded"
    assert incomplete == "expanded"
