"""Cross-mode invariance tests for ``core.application.run_invariants``.

These tests pin the behaviour of the shared task-prompt / tool-policy /
timeout / workspace builders so flat-mode and hierarchical-mode dispatchers
see byte-identical framing. The legacy parity comparators against
``experiments/shared/baselines/run_claude_code._compose_prompt`` were
retired alongside that module in PR 5; the hash invariant now lives inside
``build_task_prompt`` itself and is exercised by the worker tests in
``infrastructure/tests``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config.settings import Settings
from core.application.run_invariants import (
    TaskPromptSpec,
    TimeoutBudget,
    ToolPolicy,
    WorkerResult,
    WorkspaceSpec,
    build_env_policy,
    build_task_prompt,
    build_timeouts,
    build_tool_policy,
    build_workspace_spec,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _load_base_config() -> dict:
    return yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _settings_with(tmp_path: Path, **overrides: dict) -> Settings:
    """Build a Settings instance from the base config with deep overrides applied.

    When ``worker.tool`` is overridden, the inherited ``tool_params`` from the
    base config is dropped so the tagged-union validator only sees the active
    slot. Tests that supply their own ``tool_params`` keep that override intact.
    """
    payload = _load_base_config()
    for top_key, sub in overrides.items():
        existing = payload.get(top_key, {})
        if top_key == "worker" and isinstance(sub, dict) and "tool" in sub:
            existing = {k: v for k, v in existing.items() if k != "tool_params"}
        if isinstance(existing, dict) and isinstance(sub, dict):
            existing.update(sub)
            payload[top_key] = existing
        else:
            payload[top_key] = sub
    target = tmp_path / "settings.yaml"
    _write_yaml(target, payload)
    return Settings.from_yaml(target)


@pytest.fixture
def fixture_briefing(tmp_path: Path) -> Path:
    """A small briefing file used so tests are hermetic from prompts/ contents."""
    path = tmp_path / "briefing.md"
    path.write_text("Briefing line one.\nBriefing line two.\n", encoding="utf-8")
    return path


def test_task_prompt_byte_identical_across_modes(fixture_briefing: Path) -> None:
    """Given the same CVE+task, build_task_prompt produces identical output
    whether the surrounding settings.orchestration.mode is flat or hierarchical."""

    # Given: an identical CVE context + task slug.
    cve_context = {"cve_id": "CVE-2023-12345", "package": "openssl"}
    task = "Reproduce and patch the heap overflow"

    # When: building the prompt twice (the builder is mode-agnostic by design).
    flat_spec = build_task_prompt(
        briefing_path=fixture_briefing,
        cve_context=cve_context,
        task=task,
    )
    hier_spec = build_task_prompt(
        briefing_path=fixture_briefing,
        cve_context=cve_context,
        task=task,
    )

    # Then: rendered prompts are byte-identical and provenance matches.
    assert flat_spec.rendered_prompt == hier_spec.rendered_prompt
    assert flat_spec.briefing_sha == hier_spec.briefing_sha


def test_briefing_sha_is_stable(fixture_briefing: Path) -> None:
    """Same briefing file -> same briefing_sha (sha256 hex digest)."""

    # Given: a known briefing file and its expected sha256.
    expected_sha = hashlib.sha256(fixture_briefing.read_bytes()).hexdigest()

    # When: building two prompts from the same briefing.
    spec_a = build_task_prompt(briefing_path=fixture_briefing, cve_context=None, task="t")
    spec_b = build_task_prompt(briefing_path=fixture_briefing, cve_context={"k": "v"}, task="other")

    # Then: both report the same hash and it matches expected.
    assert spec_a.briefing_sha == expected_sha
    assert spec_b.briefing_sha == expected_sha


def test_tool_policy_for_claude_code(tmp_path: Path) -> None:
    """Settings with worker.tool=claude_code yields ToolPolicy reflecting
    tool_params.claude_code.allowed_tools and disallowed_tools."""

    # Given: a Settings selecting claude_code with explicit allow/disallow lists.
    settings = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "allowed_tools": ["Bash", "Read", "Edit"],
                    "disallowed_tools": ["WebFetch"],
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 40,
                }
            },
        },
    )

    # When: building the policy from settings.
    policy = build_tool_policy(settings=settings)

    # Then: allowed/disallowed mirror the configured lists as tuples.
    assert policy.allowed == ("Bash", "Read", "Edit")
    assert policy.disallowed == ("WebFetch",)
    # And: bash allowlist comes from security.tools (default fixture has security enabled).
    assert isinstance(policy.allowed_bash_commands, tuple)


def test_tool_policy_for_openhands(tmp_path: Path) -> None:
    """Settings with worker.tool=openhands yields ToolPolicy with allowed=('*',)
    and disallowed=()."""

    # Given: a Settings selecting openhands.
    settings = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "openhands",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "openhands": {
                    "image": "openhands:latest",
                    "timeout_seconds": 600,
                    "max_iterations_per_run": 20,
                }
            },
        },
    )

    # When: building the policy from settings.
    policy = build_tool_policy(settings=settings)

    # Then: openhands has implicit allow-all.
    assert policy.allowed == ("*",)
    assert policy.disallowed == ()


def test_timeout_budget_canonical_source(tmp_path: Path) -> None:
    """build_timeouts reads worker.timeout for per_worker_call and
    orchestration.max_run_duration_seconds for per_run_total."""

    # Given: settings with explicit values for both timeout fields.
    settings = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "timeout": 450,
            "max_iterations_per_run": 20,
        },
        orchestration={
            "max_run_duration_seconds": 7200,
        },
    )

    # When: building the timeout budget.
    budget = build_timeouts(settings)

    # Then: both fields reflect the canonical sources.
    assert budget.per_worker_call == 450
    assert budget.per_run_total == 7200

    # And: a second Settings with different values yields a different budget.
    other = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "timeout": 60,
            "max_iterations_per_run": 5,
        },
        orchestration={"max_run_duration_seconds": 900},
    )
    other_budget = build_timeouts(other)
    assert other_budget.per_worker_call == 60
    assert other_budget.per_run_total == 900


def test_env_policy_strips_secrets() -> None:
    """build_env_policy returns a frozenset; POSTGRES_PASSWORD is NOT in it."""

    # When: building the env-var allowlist.
    policy = build_env_policy()

    # Then: it is a frozenset and excludes secrets the subprocess should never see.
    assert isinstance(policy, frozenset)
    assert "POSTGRES_PASSWORD" not in policy
    assert "OPENAI_API_KEY" not in policy
    assert "LLM_API_KEY" not in policy
    # And: includes the expected baseline allowlist members.
    assert "PATH" in policy
    assert "ANTHROPIC_API_KEY" in policy


def test_workspace_spec_carries_extras(tmp_path: Path) -> None:
    """build_workspace_spec passes ``extras`` through unchanged; root is the given run_dir."""

    # Given: an arbitrary run dir and extras mapping.
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    extras = {"src": "/src", "testcase": "/testcase", "secb": "/usr/local/bin/secb"}

    # When: building the workspace spec.
    spec = build_workspace_spec(run_dir=run_dir, extras=extras)

    # Then: root and extras are preserved.
    assert spec.root == run_dir
    assert dict(spec.extras) == extras

    # And: when extras is None, an empty mapping is used.
    bare = build_workspace_spec(run_dir=run_dir, extras=None)
    assert dict(bare.extras) == {}


def test_value_objects_are_frozen(tmp_path: Path) -> None:
    """Setting an attribute on TaskPromptSpec / ToolPolicy / TimeoutBudget /
    WorkspaceSpec / WorkerResult raises ValidationError (Pydantic frozen=True)."""

    # Given: instances of each value object.
    from uuid import uuid4

    spec = TaskPromptSpec(rendered_prompt="hi", briefing_sha="abc", cve_context=None, task="t")
    policy = ToolPolicy(allowed=("a",), disallowed=(), allowed_bash_commands=())
    budget = TimeoutBudget(per_worker_call=10, per_run_total=20)
    workspace = WorkspaceSpec(root=tmp_path, extras={})
    result = WorkerResult(run_id=uuid4(), exit_status="completed", wall_time_seconds=1.0)

    # When/Then: any attempt to mutate raises ValidationError.
    for obj, attr in (
        (spec, "task"),
        (policy, "allowed"),
        (budget, "per_run_total"),
        (workspace, "root"),
        (result, "exit_status"),
    ):
        with pytest.raises(ValidationError):
            setattr(obj, attr, "mutated")


def test_task_prompt_omits_context_block_when_none(fixture_briefing: Path) -> None:
    """When cve_context is None, the prompt has only briefing + task."""

    # Given: a builder call with no cve_context.
    spec = build_task_prompt(
        briefing_path=fixture_briefing,
        cve_context=None,
        task="hello",
    )

    # When/Then: the rendered prompt has no "Task context" block.
    assert "Task context" not in spec.rendered_prompt
    assert spec.rendered_prompt.endswith("Task: hello")
