"""Catalog-wide contracts for security worker-role prompts."""

from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services import PromptBuilder
from core.domain.aggregates.agent_session import AgentRole
from core.domain.values.limits import HierarchyLimits
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import CVEInstance, SecBenchPromptStrategy
from plugins.security.prompt_strategy import _ROLE_TEMPLATE_BY_NAME
from plugins.security.roles import ROLES


PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
ANSWER_SENTINEL = "BUG_REPORT_ANSWER_KEY_SENTINEL"
DESCRIPTION_SENTINEL = "BUG_DESCRIPTION_PROMPT_SAFE_SENTINEL"
SANITIZER_SENTINEL = "SANITIZER_REPORT_PROMPT_SAFE_SENTINEL"


def _cve() -> CVEInstance:
    return CVEInstance(
        instance_id="demo.cve-2026-0001",
        repo="demo/project",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description=DESCRIPTION_SENTINEL,
        sanitizer_report=SANITIZER_SENTINEL,
        bug_report=ANSWER_SENTINEL,
        base_commit="a" * 40,
    )


def _builder() -> PromptBuilder:
    return PromptBuilder(
        template_dir=PROMPTS_DIR,
        default_tool="openhands",
        strategy=SecBenchPromptStrategy(),
    )


def _role_prompt(role_name: str) -> str:
    return _builder().build_worker_prompt(
        task_description=f"[{role_name}] perform the assigned role",
        agent_id=uuid4(),
        domain_context=_cve(),
    )


def test_role_template_allowlist_is_bijective_with_catalog() -> None:
    catalog_names = {role.name for role in ROLES}
    template_paths = tuple(_ROLE_TEMPLATE_BY_NAME.values())

    assert set(_ROLE_TEMPLATE_BY_NAME) == catalog_names
    assert len(template_paths) == len(set(template_paths)) == 16
    for template_path in template_paths:
        assert (PROMPTS_DIR / template_path).is_file(), template_path


def test_every_catalog_role_renders_one_explicit_body() -> None:
    for role in ROLES:
        prompt = _role_prompt(role.name)
        assert f"## Catalog role: [{role.name}]" in prompt
        assert f"### Your role: [{role.name}]" in prompt
        assert prompt.count("### Your role:") == 1
        assert "<role_contract>" in prompt


def test_role_contract_renders_ownership_without_blocking_explicit_fallback() -> None:
    prompt = _role_prompt("Build-Verifier")
    normalized = " ".join(prompt.split())

    assert "Foreign contract outputs — do not create or edit unless" in prompt
    assert "explicitly authorizes a narrow evidence-bound correction" in normalized
    assert "apply only the smallest correction" in prompt


def test_unknown_bracket_role_fails_closed() -> None:
    with pytest.raises(ValueError, match=r"Unknown SEC-bench worker role label \[Unknown-Role\]"):
        _builder().build_worker_prompt(
            task_description="[Unknown-Role] fix the patch",
            domain_context=_cve(),
        )


def test_case_variant_catalog_role_keeps_explicit_template() -> None:
    prompt = _builder().build_worker_prompt(
        task_description="[poc-researcher] map the input",
        domain_context=_cve(),
    )

    assert "### Your role: [PoC-Researcher]" in prompt


def test_solver_prompts_exclude_bug_report_but_keep_safe_context() -> None:
    cve = _cve()
    builder = _builder()
    limits = HierarchyLimits(
        current_depth=0,
        max_depth=2,
        max_children_per_node=7,
        max_retries=0,
        root_id=uuid4(),
        max_total_agents=30,
        current_total_agents=1,
        domain_context=cve,
    )
    briefing = Briefing(
        parent_task="security task",
        parent_role="boss",
        ancestry=(
            Ancestor(
                agent_id=str(uuid4()),
                role="boss",
                task_summary="[Builder] prepare the target",
            ),
        ),
    )
    prompts = [
        builder.build_assessment_prompt(
            task_description="assess the task",
            agent_id=uuid4(),
            domain_context=cve,
        ),
        builder.build_boss_delegation_prompt(
            task_description="delegate the task",
            agent_id=uuid4(),
            hierarchy_limits=limits,
            domain_context=cve,
        ),
        builder.build_manager_decomposition_prompt(
            task_description="[Builder] recover the phase",
            agent_id=uuid4(),
            agent_role=AgentRole.MANAGER,
            domain_context=cve,
            briefing=briefing,
        ),
        builder.build_flat_prompt(
            task_description="solve the task",
            agent_id=uuid4(),
            domain_context=cve,
        ),
        *(_role_prompt(role.name) for role in ROLES),
    ]

    for prompt in prompts:
        assert ANSWER_SENTINEL not in prompt
        assert DESCRIPTION_SENTINEL in prompt
        assert SANITIZER_SENTINEL in prompt


def test_procedure_roles_use_neutral_first_run_language() -> None:
    for role_name in (
        "Build-Verifier",
        "Exploit-Validator",
        "Patch-Applier",
        "Patch-Validator",
    ):
        prompt = _role_prompt(role_name).lower()
        assert "trusted host" not in prompt
        assert "host procedure failed once" not in prompt


def test_reproducer_and_validation_contracts_match_host_semantics() -> None:
    build = _role_prompt("Build-Executor")
    repro = _role_prompt("Repro-Creator")
    exploit = _role_prompt("Exploit-Validator")
    patch = _role_prompt("Patch-Validator")
    root_cause = _role_prompt("Root-Cause-Analyst")
    normalized_build = " ".join(build.split())

    assert "under `/src` or `/work`" in normalized_build
    assert "Do not copy or declare a `/testcase` snapshot" in normalized_build
    assert 'BIN="$(head -n 1 /testcase/binary_paths.txt)"' in repro
    assert 'POC="$(head -n 1 /testcase/poc_path.txt)"' in repro
    assert 'exec "$BIN" -v "$POC" /dev/null' in repro
    assert 'exec "$BIN" "$POC" -o /dev/null' in repro
    assert "Exit\n   0 is valid" in exploit
    assert "exact agreement on all three fields" in exploit
    assert "either 0 or the frozen dataset exit oracle" in patch
    assert "/testcase/exploit_input_identity.txt" not in root_cause
    assert "Host-issued value" not in root_cause


def test_role_prompt_rendering_is_context_deterministic_and_bounded() -> None:
    first = {role.name: _role_prompt(role.name) for role in ROLES}
    second = {role.name: _role_prompt(role.name) for role in ROLES}

    assert first == second
    assert max(len(prompt) for prompt in first.values()) < 20_000
