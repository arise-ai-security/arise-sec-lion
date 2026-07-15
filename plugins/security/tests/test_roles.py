"""Catalog invariants for plugins/security/roles.py and prompt derivations."""

from plugins.security import roles as R
from plugins.security.deliverables import (
    ARTIFACT_DIRS,
    ARTIFACT_PATHS,
    REQUIRED_FILES,
    VALIDATION_REQUIRED,
)


def _producers(path: str) -> list[str]:
    """Role names whose produces spec matches a concrete path (``*`` = prefix glob)."""
    return [
        role.name
        for role in R.ROLES
        for spec in role.produces
        if (path == spec or (spec.endswith("*") and path.startswith(spec[:-1])))
    ]


def test_every_mandated_deliverable_has_exactly_one_producer() -> None:
    """Each file the contract mandates must be owned by exactly one catalog role, so the
    eval layer can attribute it and a downstream consumer always has a producer."""
    mandated = {
        path
        for group in (REQUIRED_FILES, VALIDATION_REQUIRED)
        for paths in group.values()
        for path in paths
    }
    for path in mandated:
        owners = _producers(path)
        assert len(owners) == 1, (path, owners)


def test_root_cause_and_patch_plan_are_common_fixer_deliverables() -> None:
    """Flat and decomposed executions share one Fixer artifact contract."""
    assert REQUIRED_FILES["Fixer"] == (
        "/testcase/root_cause_analysis.txt",
        "/testcase/patch_plan.json",
        "/testcase/model_patch.diff",
    )
    assert "/testcase/fix_summary.md" not in REQUIRED_FILES["Fixer"]


def test_hard_dependencies_match_develop_prompt_artifact_handoffs() -> None:
    """Hard edges only describe required producer artifacts from the old prompt contract."""
    expected = {
        "Build-Setup": (),
        "Build-Executor": ("Build-Setup",),
        "Build-Verifier": ("Build-Executor",),
        "PoC-Researcher": (),
        "Data-Flow-Analyst": (),
        "PoC-Tester": (),
        "Forward-Instrumentator": ("PoC-Researcher",),
        "Repro-Creator": (),
        "Exploit-Validator": ("Repro-Creator",),
        "Root-Cause-Analyst": (),
        "Candidate-Reviewer": ("Root-Cause-Analyst",),
        "Regression-Tester": (),
        "Patch-Applier": ("Root-Cause-Analyst",),
        "Patch-Validator": ("Patch-Applier",),
        "Fix-Aggregator": ("Patch-Validator",),
        "Reporter": (),
    }

    for role in R.ROLES:
        assert role.depends_on == expected[role.name]


def test_soft_dependencies_match_develop_prompt_fallback_handoffs() -> None:
    """Soft edges are needed only where old prompts said to reuse evidence if present."""
    expected = {
        "PoC-Tester": ("PoC-Researcher",),
        "Repro-Creator": ("PoC-Researcher", "PoC-Tester"),
        "Root-Cause-Analyst": ("Forward-Instrumentator",),
        "Regression-Tester": ("Candidate-Reviewer",),
        "Reporter": ("Patch-Validator", "Exploit-Validator", "Build-Verifier"),
    }

    for role in R.ROLES:
        assert role.soft_depends_on == expected.get(role.name, ())


def test_every_phase_has_a_required_core_chain() -> None:
    for phase in R.PHASE_ORDER:
        assert any(role.required for role in R.PHASE_ROLES[phase]), phase


def test_artifact_path_constants_cover_mandated_deliverables() -> None:
    mandated = {
        path
        for group in (REQUIRED_FILES, VALIDATION_REQUIRED)
        for paths in group.values()
        for path in paths
    }
    concrete_mandated = {path for path in mandated if "*" not in path}

    assert ARTIFACT_DIRS["source"] == "/src"
    assert ARTIFACT_DIRS["testcase"] == "/testcase"
    assert ARTIFACT_DIRS["work"] == "/work"
    assert ARTIFACT_DIRS["default_binary"] == "/work/bin"
    assert concrete_mandated <= set(ARTIFACT_PATHS.values())
    assert ARTIFACT_PATHS["binary_paths"] == "/testcase/binary_paths.txt"
    assert ARTIFACT_PATHS["poc_path"] == "/testcase/poc_path.txt"


def test_artifact_contract_uses_selected_poc_pointer_not_package_or_glob() -> None:
    """The mandatory artifact contract must not depend on optional package evidence or
    poc* naming; patch images can ship PoCs under arbitrary filenames."""

    # Given: the flattened mandatory phase contract.
    mandated = {
        path
        for group in (REQUIRED_FILES, VALIDATION_REQUIRED)
        for paths in group.values()
        for path in paths
    }

    # Then: Builder hands exact executable paths to Exploiter.
    assert "/testcase/binary_paths.txt" in REQUIRED_FILES["Builder"]

    # And: Exploiter records the selected PoC explicitly without a filename glob.
    assert "/testcase/poc_path.txt" in REQUIRED_FILES["Exploiter"]
    assert "/testcase/poc*" not in mandated
    assert "/testcase/packages.txt" not in mandated
