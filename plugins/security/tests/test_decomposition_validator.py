"""Tests for SEC-bench Manager decomposition validation."""

from plugins.security.cve_instance import CVEInstance
from plugins.security.decomposition_validator import SecBenchDecompositionValidator
from plugins.security.plugin import SecurityDomainPlugin
from plugins.security.route_catalog import phase_controller_instruction, role_instruction


EXPLOITER_COMPACT = (
    "Repro-Creator",
    "Exploit-Validator",
)
BUILDER_TASK = phase_controller_instruction("Builder")
EXPLOITER_TASK = phase_controller_instruction("Exploiter")
FIXER_TASK = phase_controller_instruction("Fixer")
REPORTER_TASK = phase_controller_instruction("Reporter")


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


def _adaptive_validator(
    policy_version: str = "policy-v2",
) -> SecBenchDecompositionValidator:
    return SecBenchDecompositionValidator(policy_version=policy_version)


def test_complete_compact_exploiter_decomposition_is_ok() -> None:
    v = SecBenchDecompositionValidator()
    subs = tuple(f"[{r}] do {r}" for r in EXPLOITER_COMPACT)
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions
    assert not verdict.removals


def test_missing_exploit_validator_is_injected() -> None:
    v = SecBenchDecompositionValidator()
    subs = tuple(f"[{r}] do {r}" for r in EXPLOITER_COMPACT if r != "Exploit-Validator")
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert not verdict.removals
    assert any(a.description.startswith("[Exploit-Validator]") for a in verdict.additions)
    assert any(viol.kind == "missing_required_role" for viol in verdict.violations)


def test_merged_repro_validator_leaf_is_split() -> None:
    v = SecBenchDecompositionValidator()
    subs = (
        "[PoC-Researcher] map",
        "[Data-Flow-Analyst] trace",
        "[PoC-Tester] run",
        "[Forward-Instrumentator] probe",
        "[Repro-Creator] write repro.sh and [Exploit-Validator] validate and write verdict",
    )
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert 4 in verdict.removals
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
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="Reproduce and fix the CVE",
        subtask_descriptions=("[Builder] x",),
        domain_context=_cve(),
    )
    assert verdict.ok


def test_phase_resolved_only_from_leading_bracket() -> None:
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="Investigate the [Exploiter] phase artifacts",
        subtask_descriptions=("[PoC-Researcher] x",),
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions


def test_role_bracket_parent_is_noop() -> None:
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description="[Repro-Creator] write the repro script",
        subtask_descriptions=("[PoC-Tester] run the poc",),
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions


def test_reporter_leaf_is_not_misclassified_as_reporter_controller() -> None:
    verdict = SecBenchDecompositionValidator().classify(
        parent_task_description=role_instruction("Reporter"),
        subtask_descriptions=("[Not-A-Real-Role] should be ignored",),
        domain_context=_cve(),
    )

    assert verdict.ok
    assert not verdict.additions
    assert not verdict.removals


def test_injected_leaf_carries_insight_and_validator_exposes_hard_deps() -> None:
    v = SecBenchDecompositionValidator()
    subs = tuple(f"[{r}] do {r}" for r in EXPLOITER_COMPACT if r != "Exploit-Validator")
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    ev = next(a for a in verdict.additions if a.description.startswith("[Exploit-Validator]"))
    assert "deterministic" in ev.description.lower()
    assert v.hard_dependencies("Exploit-Validator") == ("Repro-Creator",)
    assert v.hard_dependencies("PoC-Researcher") == ()
    assert v.hard_dependencies("Reporter") == (
        "Builder",
        "Exploiter",
        "Fixer",
        "Build-Verifier",
        "Exploit-Validator",
        "Patch-Validator",
    )


def test_builder_phase_required_roles_enforced() -> None:
    v = SecBenchDecompositionValidator()
    subs = ("[Build-Setup] x", "[Build-Executor] x")
    verdict = v.classify(
        parent_task_description=BUILDER_TASK,
        subtask_descriptions=subs,
        domain_context=_cve(),
    )
    assert not verdict.ok
    assert any(a.description.startswith("[Build-Verifier]") for a in verdict.additions)


def test_initial_decomposition_enforces_compact_roles() -> None:
    v = SecBenchDecompositionValidator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=("[Repro-Creator] x", "[Exploit-Validator] y"),
        domain_context=_cve(),
    )
    assert verdict.ok
    assert not verdict.additions


def test_initial_decomposition_removes_optional_specialist() -> None:
    verdict = SecBenchDecompositionValidator().classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[PoC-Researcher] research",
            "[Repro-Creator] create",
            "[Exploit-Validator] validate",
        ),
        domain_context=_cve(),
    )

    assert not verdict.ok
    assert verdict.removals == (0,)
    assert any(v.kind == "disallowed_role" for v in verdict.violations)


def test_security_plugin_propagates_route_policy_version() -> None:
    # Given: A security plugin with explicit route provenance
    plugin = SecurityDomainPlugin(route_policy_version="policy-v3")

    # When: The plugin constructs its initial policy and adaptive validator
    policy = plugin.get_decomposition_policy()
    initial = policy.select_initial(
        parent_task_description="Repair the CVE",
        domain_context=_cve(),
        is_root=True,
        redecomposition_count=0,
    )
    validator = plugin.get_decomposition_validator()
    assert validator is not None
    adaptive = validator.classify(
        parent_task_description=REPORTER_TASK,
        subtask_descriptions=("[Reporter] revise the failed report",),
        domain_context=_cve(),
        redecomposition_count=1,
        redecomposition_limit=2,
    )

    # Then: Both route sources stamp the configured policy version
    assert initial is not None
    assert initial.policy_version == "policy-v3"
    assert adaptive.policy_version == "policy-v3"


def test_manager_specialists_accepted_with_escalated_route() -> None:
    v = _adaptive_validator()
    originals = (
        "[PoC-Researcher] map ops to functions from the digest",
        "[Data-Flow-Analyst] trace the conflict path",
        "[Repro-Creator] rewrite repro for the selected PoC",
        "[Exploit-Validator] re-validate determinism",
    )
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=originals,
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Repro-Creator", "Exploit-Validator"),
        completed_role_labels=(),
    )
    assert verdict.route == "escalated"
    assert "manager_llm" in verdict.triggers
    assert verdict.phase == "Exploiter"
    labels = _final_labels(verdict, originals)
    assert "PoC-Researcher" in labels
    assert "Data-Flow-Analyst" in labels
    assert "Repro-Creator" in labels


def test_accepted_role_label_is_canonicalized_without_losing_instruction() -> None:
    verdict = _adaptive_validator().classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=("investigate [poc-researcher] using failure.log",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )

    assert verdict.validated_subtasks[0].canonical_description == (
        "[PoC-Researcher] investigate using failure.log"
    )


def test_two_manager_decisions_yield_different_specialist_sets() -> None:
    v = _adaptive_validator()
    a = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[PoC-Researcher] research",
            "[PoC-Tester] test",
            "[Repro-Creator] repro",
            "[Exploit-Validator] validate",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )
    b = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[Data-Flow-Analyst] trace",
            "[Forward-Instrumentator] probe",
            "[Repro-Creator] repro",
            "[Exploit-Validator] validate",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )
    labels_a = _final_labels(a, (
        "[PoC-Researcher] research",
        "[PoC-Tester] test",
        "[Repro-Creator] repro",
        "[Exploit-Validator] validate",
    ))
    labels_b = _final_labels(b, (
        "[Data-Flow-Analyst] trace",
        "[Forward-Instrumentator] probe",
        "[Repro-Creator] repro",
        "[Exploit-Validator] validate",
    ))
    assert labels_a != labels_b
    assert "PoC-Tester" in labels_a
    assert "Data-Flow-Analyst" in labels_b
    assert "Forward-Instrumentator" in labels_b
    # Forward-Instrumentator requires PoC-Researcher
    assert "PoC-Researcher" in labels_b


def test_keywords_in_stdout_do_not_affect_routing() -> None:
    # Validator has no failure_signal / keyword path — identical inputs → identical output.
    v = _adaptive_validator()
    kwargs = dict(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=("[Repro-Creator] x", "[Exploit-Validator] y"),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )
    first = v.classify(**kwargs)
    second = v.classify(**kwargs)
    assert first.route == second.route
    assert first.additions == second.additions
    assert first.removals == second.removals


def test_cross_phase_role_is_removed() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[Root-Cause-Analyst] wrong phase",
            "[Repro-Creator] ok",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Repro-Creator",),
    )
    assert 0 in verdict.removals
    assert any(viol.kind == "invalid_role" for viol in verdict.violations)
    assert verdict.route == "escalated"


def test_mixed_phase_leaf_is_rejected_before_phase_filtering() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[PoC-Researcher] investigate and [Build-Setup] rebuild",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )

    assert verdict.removals == (0,)
    assert any(violation.kind == "merged_leaf" for violation in verdict.violations)
    labels = {addition.description.split("]")[0][1:] for addition in verdict.additions}
    assert "PoC-Researcher" in labels
    assert "Build-Setup" not in labels


def test_unknown_role_triggers_fallback_when_nothing_valid_remains() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=("[Not-A-Real-Role] invent",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )
    assert verdict.route == "expanded"
    assert "fallback_expanded" in verdict.triggers
    labels = {a.description.split("]")[0][1:] for a in verdict.additions}
    assert "Repro-Creator" in labels
    assert "Exploit-Validator" in labels
    assert "PoC-Researcher" in labels


def test_merged_roles_split_on_redecomposition() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[Repro-Creator] write and [Exploit-Validator] validate",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )
    assert 0 in verdict.removals
    labels = {a.description.split("]")[0][1:] for a in verdict.additions}
    assert "Repro-Creator" in labels
    assert "Exploit-Validator" in labels


def test_missing_hard_dependency_is_injected() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=("[Forward-Instrumentator] probe without producer",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
    )
    assert any(a.description.startswith("[PoC-Researcher]") for a in verdict.additions)
    assert any(viol.kind == "missing_hard_dependency" for viol in verdict.violations)
    assert verdict.route == "escalated"


def test_explicit_completed_owner_may_be_reissued_for_correction() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[Repro-Creator] repair the rejected replay artifact",
            "[PoC-Researcher] new specialist",
            "[Exploit-Validator] recheck",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
        completed_role_labels=("Repro-Creator",),
    )
    assert 0 not in verdict.removals
    assert not any(viol.kind == "completed_role" for viol in verdict.violations)


def test_unselected_completed_dependency_is_reused_without_injection() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=("[Exploit-Validator] recheck repaired evidence",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Exploit-Validator",),
        completed_role_labels=("Repro-Creator",),
    )
    assert not any(a.description.startswith("[Repro-Creator]") for a in verdict.additions)


def test_failed_compact_role_may_be_reissued() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=EXPLOITER_TASK,
        subtask_descriptions=(
            "[Repro-Creator] revised repro for nondeterministic crash",
            "[Exploit-Validator] re-validate",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Repro-Creator", "Exploit-Validator"),
        completed_role_labels=(),
    )
    assert verdict.route == "escalated"
    assert 0 not in verdict.removals  # failed compact kept


def test_reporter_failure_can_reissue_reporter() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=REPORTER_TASK,
        subtask_descriptions=("[Reporter] rewrite with evidence links",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Reporter",),
    )
    assert verdict.route == "escalated"
    assert not verdict.removals


def test_invalid_decision_expands_within_same_phase_only() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=FIXER_TASK,
        subtask_descriptions=("[Totally-Invalid] x",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Patch-Validator",),
    )
    assert verdict.route == "expanded"
    labels = {a.description.split("]")[0][1:] for a in verdict.additions}
    assert "Root-Cause-Analyst" in labels
    assert "Patch-Applier" in labels
    assert "Patch-Validator" in labels
    assert "Candidate-Reviewer" in labels or "Regression-Tester" in labels
    assert "PoC-Researcher" not in labels
    assert "Builder" not in labels


def test_phase_route_provenance_on_valid_manager_decision() -> None:
    v = _adaptive_validator()
    verdict = v.classify(
        parent_task_description=BUILDER_TASK,
        subtask_descriptions=(
            "[Build-Setup] install deps from digest",
            "[Build-Executor] rebuild",
            "[Build-Verifier] verify",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Build-Verifier",),
    )
    assert verdict.route == "escalated"
    assert verdict.triggers == ("phase_gate_failed", "manager_llm")
    assert verdict.policy_version == "policy-v2"
    assert verdict.phase == "Builder"
    assert verdict.remaining_budget == 0


def _final_labels(verdict, originals: tuple[str, ...]) -> set[str]:
    kept = {
        originals[i].split("]")[0][1:]
        for i in range(len(originals))
        if i not in verdict.removals
    }
    for add in verdict.additions:
        kept.add(add.description.split("]")[0][1:])
    return kept
