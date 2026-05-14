"""Invariant tests for ``core.application.run_invariants`` builders.

These cover the policy/timeout/workspace/value-object contracts that
survived the prompt-unification refactor. The legacy briefing-path
prompt tests were retired; the prompt-rendering invariants now live in
``plugins/security/tests/test_prompt_unification_invariants.py``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

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
    build_timeouts,
    build_tool_policy,
    build_workspace_spec,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _settings_with(tmp_path: Path, **overrides: dict) -> Settings:
    payload = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    payload.setdefault("boss", {})["model"] = "test-boss-model"
    payload.setdefault("manager", {})["model"] = "test-manager-model"
    payload.setdefault("worker", {})["model"] = "test-worker-model"
    for top_key, sub in overrides.items():
        existing = payload.get(top_key, {})
        if (
            top_key == "worker"
            and isinstance(sub, dict)
            and "tool" in sub
            and isinstance(existing, dict)
        ):
            existing = {k: v for k, v in existing.items() if k != "tool_params"}
        if isinstance(existing, dict) and isinstance(sub, dict):
            existing.update(sub)
            payload[top_key] = existing
        else:
            payload[top_key] = sub
    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return Settings.from_yaml(target)


def test_tool_policy_for_claude_code(tmp_path: Path) -> None:
    """worker.tool=claude_code yields ToolPolicy reflecting top-level lists.

    Post-BUG-A1 (2026-05-12), ``build_tool_policy`` reads tool policy from
    top-level ``worker.allowed_tools`` / ``disallowed_tools`` rather than the
    nested ``tool_params.claude_code.*`` slot. The cell yamls follow the same
    contract; this test pins it for the value object.
    """
    settings = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "allowed_tools": ["Bash", "Read", "Edit"],
            "disallowed_tools": ["WebFetch"],
            "tool_params": {
                "claude_code": {
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 40,
                }
            },
        },
    )
    policy = build_tool_policy(settings=settings)
    assert policy.allowed == ("Bash", "Read", "Edit")
    assert policy.disallowed == ("WebFetch",)
    assert isinstance(policy.allowed_bash_commands, tuple)


def test_tool_policy_for_openhands(tmp_path: Path) -> None:
    """worker.tool=openhands defaults to wildcard allow with empty disallow."""
    settings = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "openhands",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "openhands": {}
            },
        },
    )
    policy = build_tool_policy(settings=settings)
    assert policy.allowed == ("*",)
    assert policy.disallowed == ()


def test_timeout_budget_canonical_source(tmp_path: Path) -> None:
    """build_timeouts reads worker.timeout and orchestration.max_run_duration_seconds."""
    settings = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "timeout": 450,
            "max_iterations_per_run": 20,
        },
        orchestration={"max_run_duration_seconds": 7200},
    )
    budget = build_timeouts(settings)
    assert budget.per_worker_call == 450
    assert budget.per_run_total == 7200


def test_env_policy_strips_secrets() -> None:
    """build_env_policy excludes secrets the subprocess must never see."""
    policy = build_env_policy()
    assert isinstance(policy, frozenset)
    # Security-sensitive: each of these would let a subprocess exfiltrate
    # credentials. Their absence is the whole point of the allowlist.
    assert "POSTGRES_PASSWORD" not in policy
    assert "OPENAI_API_KEY" not in policy
    assert "LLM_API_KEY" not in policy
    assert "PATH" in policy
    assert "ANTHROPIC_API_KEY" in policy


def test_workspace_spec_carries_extras(tmp_path: Path) -> None:
    """build_workspace_spec passes extras through unchanged."""
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    extras = {"src": "/src", "testcase": "/testcase", "secb": "/usr/local/bin/secb"}
    spec = build_workspace_spec(run_dir=run_dir, extras=extras)
    assert spec.root == run_dir
    assert dict(spec.extras) == extras
    bare = build_workspace_spec(run_dir=run_dir, extras=None)
    assert dict(bare.extras) == {}


def test_value_objects_are_frozen(tmp_path: Path) -> None:
    """Mutation on the value objects raises ValidationError (frozen=True)."""
    spec = TaskPromptSpec(rendered_prompt="hi", prompt_sha="abc", cve_context=None, task="t")
    policy = ToolPolicy(allowed=("a",), disallowed=(), allowed_bash_commands=())
    budget = TimeoutBudget(per_worker_call=10, per_run_total=20)
    workspace = WorkspaceSpec(root=tmp_path, extras={})
    result = WorkerResult(run_id=uuid4(), exit_status="completed", wall_time_seconds=1.0)

    for obj, attr in (
        (spec, "task"),
        (policy, "allowed"),
        (budget, "per_run_total"),
        (workspace, "root"),
        (result, "exit_status"),
    ):
        with pytest.raises(ValidationError):
            setattr(obj, attr, "mutated")
