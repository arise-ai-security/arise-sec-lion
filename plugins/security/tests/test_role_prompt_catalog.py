"""Catalog-wide contracts for SEC-bench worker-role prompts."""

from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services import PromptBuilder
from plugins.security import CVEInstance, SecBenchPromptStrategy
from plugins.security.prompt_strategy import _ROLE_TEMPLATE_BY_NAME
from plugins.security.roles import ROLES


PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
ANALYSIS_ONLY_ROLES = {
    "PoC-Researcher",
    "Data-Flow-Analyst",
    "PoC-Tester",
    "Forward-Instrumentator",
    "Candidate-Reviewer",
    "Regression-Tester",
}
PROCEDURE_ROLES = {
    "Build-Verifier",
    "Exploit-Validator",
    "Patch-Applier",
    "Patch-Validator",
}


def _cve() -> CVEInstance:
    return CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="demo/project",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="Heap overflow in demo parser.",
        sanitizer_report=(
            "==1==ERROR: AddressSanitizer: heap-buffer-overflow READ\n"
            "#0 0x123 in parse_packet /src/demo/parser.c:42\n"
        ),
        bug_report="ANSWER: replace unsafe_size with checked_size in parser.c:42",
        base_commit="a" * 40,
    )


def _builder(*, role_fused: bool = False) -> PromptBuilder:
    return PromptBuilder(
        template_dir=PROMPTS_DIR,
        default_tool="openhands",
        strategy=SecBenchPromptStrategy(role_fused=role_fused),
    )


def _role_prompt(role_name: str, *, task_suffix: str = "perform the assigned role") -> str:
    return _builder().build_worker_prompt(
        task_description=f"[{role_name}] {task_suffix}",
        agent_id=uuid4(),
        domain_context=_cve(),
    )


def test_role_template_allowlist_is_bijective_with_catalog() -> None:
    # Given: the canonical role catalog and static prompt allowlist.
    catalog_names = {role.name for role in ROLES}

    # When: resolving every allowlisted template on disk.
    template_names = set(_ROLE_TEMPLATE_BY_NAME)
    template_paths = tuple(_ROLE_TEMPLATE_BY_NAME.values())

    # Then: every role maps to one distinct existing template and there are no extras.
    assert template_names == catalog_names
    assert len(template_paths) == len(set(template_paths)) == 16
    for template_path in template_paths:
        assert (PROMPTS_DIR / template_path).is_file(), template_path


def test_every_catalog_role_renders_only_its_explicit_role_body() -> None:
    # Given: all 16 canonical role labels.
    role_names = tuple(role.name for role in ROLES)

    # When: rendering each B4 leaf independently.
    prompts = {name: _role_prompt(name) for name in role_names}

    # Then: every prompt carries one catalog contract and no whole-phase contract.
    for role_name, prompt in prompts.items():
        assert f"## Catalog role: [{role_name}]" in prompt
        assert f"### Your role: [{role_name}]" in prompt
        assert prompt.count("### Your role:") == 1
        assert "### Whole-phase" not in prompt
        assert "### Goal" not in prompt


def test_role_contract_renders_catalog_ownership_and_foreign_outputs() -> None:
    # Given: each catalog role and the outputs owned by its same-phase peers.
    for role in ROLES:
        phase_roles = tuple(candidate for candidate in ROLES if candidate.phase == role.phase)
        foreign = {
            path
            for candidate in phase_roles
            if candidate.name != role.name
            for path in candidate.produces
            if path not in role.produces
        }

        # When: rendering that role's worker contract.
        prompt = _role_prompt(role.name)

        # Then: catalog-owned outputs are explicit and foreign outputs are fenced.
        for path in role.produces:
            assert path in prompt
        if foreign:
            assert "Foreign contract outputs — Do NOT create or edit these files" in prompt
            for path in foreign:
                assert path in prompt


def test_analysis_roles_write_named_evidence_without_contract_deliverables() -> None:
    # Given: catalog roles whose work is analysis-only.
    for role_name in ANALYSIS_ONLY_ROLES:
        # When: rendering the role prompt.
        prompt = _role_prompt(role_name)

        # Then: it owns no phase contract output and receives a named evidence path.
        assert "**Owned contract outputs:** none" in prompt
        assert "/testcase/" in prompt
        assert "analysis evidence rather than a phase verdict or contract deliverable" in prompt


def test_procedure_roles_are_explicit_agentic_fallbacks() -> None:
    # Given: the four host-procedure-backed roles.
    for role_name in PROCEDURE_ROLES:
        # When: rendering their retry prompt.
        prompt = _role_prompt(role_name)

        # Then: each is framed as a fallback after trusted host failure.
        assert f"### Your role: [{role_name}] agentic fallback" in prompt
        assert "trusted host" in prompt.lower()


def test_repro_creator_never_repairs_or_rewrites_builder_outputs() -> None:
    # Given: the compact B4 Repro-Creator prompt.
    prompt = _role_prompt("Repro-Creator")

    # When: checking its recovery and ownership language.
    lowered = prompt.lower()

    # Then: a broken Builder handoff is reported, never repaired in this role.
    assert "builder outputs are immutable inputs" in lowered
    assert "run `secb build`" not in lowered
    assert "rewrite `/testcase/binary_paths.txt`" not in lowered
    assert "report the broken builder handoff and stop" in lowered


def test_patch_applier_never_edits_the_approved_patch_plan() -> None:
    # Given: the Patch-Applier fallback prompt.
    prompt = _role_prompt("Patch-Applier")

    # When / Then: invalid plans return to the author; valid plans are applied literally.
    assert "Treat the approved\n`/testcase/patch_plan.json` as immutable" in prompt
    assert "do not correct, rewrite, or reinterpret it" in prompt
    assert "Root-Cause-Analyst can author a new plan" in prompt
    assert "Print the corrected plan" not in prompt


def test_reporter_reads_authoritative_artifacts_and_preserves_failures() -> None:
    # Given: the B4 Reporter role prompt.
    prompt = _role_prompt("Reporter")

    # When / Then: it reads host verdicts/raw logs and cannot infer success from summaries.
    for path in (
        "/testcase/build.log",
        "/testcase/exploit_validation_results.txt",
        "/testcase/repro_run_*.log",
        "/testcase/root_cause_analysis.txt",
        "/testcase/patch_plan.json",
        "/testcase/patch_validation_results.txt",
        "/testcase/fix_run_*.log",
    ):
        assert path in prompt
    assert "Never turn a FAIL" in prompt
    assert "Do not infer a successful fix" in prompt


def test_unknown_bracket_role_fails_closed() -> None:
    # Given: an untrusted bracket label containing a known phase keyword in its task body.
    builder = _builder()

    # When / Then: it cannot fall through to a whole-phase prompt.
    with pytest.raises(ValueError, match=r"Unknown SEC-bench worker role label \[Unknown-Role\]"):
        builder.build_worker_prompt(
            task_description="[Unknown-Role] fix the patch",
            domain_context=_cve(),
        )


def test_role_fused_phase_prompts_keep_host_procedure_authority() -> None:
    # Given: B3's four canonical phase workers.
    builder = _builder(role_fused=True)
    prompts = {
        phase: builder.build_worker_prompt(
            task_description=f"[{phase}] execute the fused phase",
            domain_context=_cve(),
        )
        for phase in ("Builder", "Exploiter", "Fixer", "Reporter")
    }

    # When / Then: producer phases remain fused and reserve mechanical authority for hosts.
    assert "Role-fused Builder" in prompts["Builder"]
    assert "Do not decide the final build PASS/FAIL" in prompts["Builder"]
    assert "Role-fused Exploiter" in prompts["Exploiter"]
    assert "Do not write `/testcase/exploit_validation_results.txt`" in prompts["Exploiter"]
    assert "Role-fused Fixer" in prompts["Fixer"]
    assert "Do not create `/testcase/model_patch.diff`" in prompts["Fixer"]
    assert "<role_contract>" not in "\n".join(prompts.values())


def test_role_prefix_is_stable_and_prompts_stay_bounded() -> None:
    # Given: two tasks for every role on the same CVE.
    prompts_a = {role.name: _role_prompt(role.name, task_suffix="attempt A") for role in ROLES}
    prompts_b = {role.name: _role_prompt(role.name, task_suffix="attempt B") for role in ROLES}

    # When: splitting at the volatile task block and the role-specific seam.
    stable_a = {name: prompt.split("\n\n<task>", 1)[0] for name, prompt in prompts_a.items()}
    stable_b = {name: prompt.split("\n\n<task>", 1)[0] for name, prompt in prompts_b.items()}
    common = {
        prompt.split("\n\n<role_contract>", 1)[0]
        for prompt in prompts_a.values()
    }

    # Then: retries are byte-stable, all roles share one substantial prefix, and no prompt bloats.
    assert stable_a == stable_b
    assert len(common) == 1
    assert len(common.pop()) > 5_000
    assert max(len(prompt) for prompt in prompts_a.values()) < 20_000
