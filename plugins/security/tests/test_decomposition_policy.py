"""Tests for deterministic SEC-bench initial routes and adaptive validation."""

from unittest.mock import MagicMock
from uuid import uuid4

from core.application.services.orchestration.decomposition_contract import (
    DecompositionContractService,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.values.limits import HierarchyLimits
from plugins.security.cve_instance import CVEInstance
from plugins.security.decomposition_policy import SecBenchInitialDecompositionPolicy
from plugins.security.decomposition_validator import SecBenchDecompositionValidator
from plugins.security.route_catalog import phase_controller_instruction, role_instruction


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


def _labels(descriptions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(description.split("]", maxsplit=1)[0][1:] for description in descriptions)


def test_root_policy_returns_exact_phase_dag() -> None:
    # Given: The deterministic initial SEC-bench policy
    policy = SecBenchInitialDecompositionPolicy("policy-v3")

    # When: The root decomposition is selected
    selection = policy.select_initial(
        parent_task_description="Repair the CVE",
        domain_context=_cve(),
        is_root=True,
        redecomposition_count=0,
    )

    # Then: The four phases and their dependency indices match the canonical DAG
    assert selection is not None
    assert _labels(tuple(subtask.description for subtask in selection.subtasks)) == (
        "Builder",
        "Exploiter",
        "Fixer",
        "Reporter",
    )
    assert tuple(subtask.depends_on for subtask in selection.subtasks) == (
        (),
        (0,),
        (1,),
        (0, 1, 2),
    )
    assert selection.policy_version == "policy-v3"
    assert selection.phase == "skeleton"
    assert selection.route == "compact"
    assert selection.triggers == ("run_started",)


def test_policy_returns_exact_compact_phase_routes_and_dependencies() -> None:
    # Given: The expected dependency-closed compact route for each phase
    policy = SecBenchInitialDecompositionPolicy("policy-v3")
    expected = {
        "Builder": (
            ("Build-Setup", "Build-Executor", "Build-Verifier"),
            ((), (0,), (1,)),
        ),
        "Exploiter": (
            ("Repro-Creator", "Exploit-Validator"),
            ((), (0,)),
        ),
        "Fixer": (
            ("Root-Cause-Analyst", "Patch-Applier", "Patch-Validator"),
            ((), (0,), (1,)),
        ),
        "Reporter": (("Reporter",), ((),)),
    }

    # When: Each phase selects its initial route
    selections = {
        phase: policy.select_initial(
            parent_task_description=phase_controller_instruction(phase),
            domain_context=_cve(),
            is_root=False,
            redecomposition_count=0,
        )
        for phase in expected
    }

    # Then: Every phase has the exact compact role order and dependency indices
    for phase, (expected_labels, expected_dependencies) in expected.items():
        selection = selections[phase]
        assert selection is not None
        assert _labels(tuple(subtask.description for subtask in selection.subtasks)) == (
            expected_labels
        )
        assert tuple(subtask.depends_on for subtask in selection.subtasks) == (
            expected_dependencies
        )
        assert selection.policy_version == "policy-v3"
        assert selection.phase == phase
        assert selection.route == "compact"
        assert selection.triggers == ("phase_started",)


def test_phase_redecomposition_is_left_to_manager() -> None:
    # Given: A phase that has already attempted its deterministic compact route
    policy = SecBenchInitialDecompositionPolicy("policy-v3")

    # When: Initial policy selection is requested during re-decomposition
    selection = policy.select_initial(
        parent_task_description=phase_controller_instruction("Exploiter"),
        domain_context=_cve(),
        is_root=False,
        redecomposition_count=1,
    )

    # Then: The host does not semantically select the adaptive route
    assert selection is None


def test_root_redecomposition_has_no_host_or_llm_route() -> None:
    policy = SecBenchInitialDecompositionPolicy("policy-v3")

    selection = policy.select_initial(
        parent_task_description="Repair the CVE",
        domain_context=_cve(),
        is_root=True,
        redecomposition_count=1,
    )

    assert selection is None


def test_manager_layer_omission_flattens_the_same_compact_route() -> None:
    policy = SecBenchInitialDecompositionPolicy("policy-v3")
    root_id = uuid4()
    root = AgentSession.create(
        agent_id=root_id,
        parent_id=None,
        role=AgentRole.BOSS,
        config={
            "strategy": "heuristic",
            "base": {"model": "m", "temperature": 0.5, "max_tokens": 4000},
            "tool": "claude_code",
        },
    )
    root.assign_task("Repair the defect")
    root.hierarchy_limits = HierarchyLimits(
        root_id=root_id,
        current_depth=0,
        max_depth=1,
        max_children_per_node=9,
        max_retries=1,
        max_total_agents=30,
        current_total_agents=1,
        domain_context=_cve(),
    )
    service = DecompositionContractService(
        MagicMock(),
        decomposition_policy=policy,
        decomposition_validator=SecBenchDecompositionValidator(),
        include_manager_layer=False,
        max_redecompositions=1,
    )

    initial = service.initial_decomposition(root)

    assert initial is not None
    subtasks, route = initial
    assert _labels(tuple(subtask.description for subtask in subtasks)) == (
        "Build-Setup",
        "Build-Executor",
        "Build-Verifier",
        "Repro-Creator",
        "Exploit-Validator",
        "Root-Cause-Analyst",
        "Patch-Applier",
        "Patch-Validator",
        "Reporter",
    )
    assert tuple(tuple(subtask.depends_on) for subtask in subtasks) == (
        (),
        (0,),
        (1,),
        (2,),
        (3,),
        (4,),
        (5,),
        (6,),
        (2, 4, 7),
    )
    resolved = service.resolve_catalog_dependencies(subtasks)
    assert tuple(tuple(subtask.depends_on) for subtask in resolved) == (
        (),
        (0,),
        (1,),
        (2,),
        (3,),
        (4,),
        (5,),
        (6,),
        (2, 4, 7),
    )
    assert route.triggers == ("run_started", "manager_layer_omitted")

    reporter = subtasks[-1]
    assert reporter.description.startswith("[Reporter]")
    assert reporter.estimated_complexity == "simple"
    assert (
        policy.select_initial(
            parent_task_description=role_instruction("Reporter"),
            domain_context=_cve(),
            is_root=False,
            redecomposition_count=0,
        )
        is None
    )


def test_initial_validator_rejects_duplicate_role_leaf() -> None:
    # Given: An initial compact route with a duplicate Repro-Creator leaf
    validator = SecBenchDecompositionValidator()

    # When: The host validates the proposed route
    verdict = validator.classify(
        parent_task_description=phase_controller_instruction("Exploiter"),
        subtask_descriptions=(
            "[Repro-Creator] first attempt",
            "[Repro-Creator] duplicate attempt",
            "[Exploit-Validator] validate",
        ),
        domain_context=_cve(),
    )

    # Then: The duplicate is removed without removing the first leaf
    assert not verdict.ok
    assert verdict.removals == (1,)
    assert any(violation.kind == "duplicate_role" for violation in verdict.violations)


def test_adaptive_validator_rejects_duplicate_role_leaf() -> None:
    # Given: A Manager-authored adaptive route with a duplicate specialist leaf
    validator = SecBenchDecompositionValidator()

    # When: The host validates the proposed route
    verdict = validator.classify(
        parent_task_description=phase_controller_instruction("Exploiter"),
        subtask_descriptions=(
            "[PoC-Researcher] inspect the digest",
            "[PoC-Researcher] duplicate inspection",
        ),
        domain_context=_cve(),
        redecomposition_count=1,
        redecomposition_limit=2,
    )

    # Then: The duplicate is removed and structural repair is recorded
    assert verdict.removals == (1,)
    assert any(violation.kind == "duplicate_role" for violation in verdict.violations)
    assert "host_structural_repair" in verdict.triggers


def test_completed_role_history_is_case_insensitive() -> None:
    # Given: A completed role recorded with non-canonical casing
    validator = SecBenchDecompositionValidator()

    # When: The Manager attempts to respawn the canonical role
    verdict = validator.classify(
        parent_task_description=phase_controller_instruction("Exploiter"),
        subtask_descriptions=("[Repro-Creator] retry completed work",),
        domain_context=_cve(),
        redecomposition_count=1,
        completed_role_labels=("repro-creator",),
        redecomposition_limit=2,
    )

    # Then: The completed role is removed despite the case difference
    assert verdict.removals == (0,)
    assert any(violation.kind == "completed_role" for violation in verdict.violations)
    assert not any(
        addition.description.startswith("[Repro-Creator]")
        for addition in verdict.additions
    )


def test_failed_role_history_is_case_insensitive() -> None:
    # Given: A previously failed optional role recorded with non-canonical casing
    validator = SecBenchDecompositionValidator()

    # When: The Manager reissues the canonical optional role
    verdict = validator.classify(
        parent_task_description=phase_controller_instruction("Fixer"),
        subtask_descriptions=("[Fix-Aggregator] revise the failed synthesis",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("fix-aggregator",),
        redecomposition_limit=2,
    )

    # Then: The role is allowed as failed history rather than rejected as outside the route
    assert verdict.removals == ()
    assert not any(violation.kind == "disallowed_role" for violation in verdict.violations)
    assert verdict.route == "escalated"


def test_route_reports_actual_remaining_redecomposition_budget() -> None:
    # Given: A three-attempt budget with one re-decomposition already consumed
    validator = SecBenchDecompositionValidator(policy_version="policy-v3")

    # When: The Manager's adaptive route is validated
    verdict = validator.classify(
        parent_task_description=phase_controller_instruction("Reporter"),
        subtask_descriptions=("[Reporter] revise with the failed evidence",),
        domain_context=_cve(),
        redecomposition_count=1,
        failed_role_labels=("Reporter",),
        redecomposition_limit=3,
    )

    # Then: Provenance reports the configured budget rather than a fixed one-attempt budget
    assert verdict.policy_version == "policy-v3"
    assert verdict.remaining_budget == 2
