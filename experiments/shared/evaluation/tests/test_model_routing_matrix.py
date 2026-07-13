"""Resolve worker models for B4 roles and B3's matching direct compact route."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.overlay import resolve_overlay
from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter


REPO_ROOT = Path(__file__).resolve().parents[4]
B4_CONFIG = REPO_ROOT / "experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml"
B3_CONFIG = REPO_ROOT / "experiments/b3-direct-compact/configs/B3-direct-compact.yaml"

B4_CODING = (
    "[Build-Setup]",
    "[Build-Executor]",
    "[Repro-Creator]",
)
B4_REASONING = (
    "[PoC-Researcher]",
    "[Data-Flow-Analyst]",
    "[PoC-Tester]",
    "[Forward-Instrumentator]",
    "[Root-Cause-Analyst]",
    "[Candidate-Reviewer]",
    "[Regression-Tester]",
    "[Fix-Aggregator]",
    "[Reporter]",
)
B4_PROCEDURE = (
    "[Build-Verifier]",
    "[Exploit-Validator]",
    "[Patch-Applier]",
    "[Patch-Validator]",
)
B3_DIRECT_COMPACT = (
    "[Build-Setup]",
    "[Build-Executor]",
    "[Build-Verifier]",
    "[Repro-Creator]",
    "[Exploit-Validator]",
    "[Root-Cause-Analyst]",
    "[Patch-Applier]",
    "[Patch-Validator]",
    "[Reporter]",
)


def _adapter_from_overlay(path: Path) -> OpenHandsAdapter:
    # Resolve the overlay only — full Settings.from_yaml requires DB secrets.
    raw = resolve_overlay(path, repo_root=REPO_ROOT)
    worker = raw["worker"]
    return OpenHandsAdapter(
        model=str(worker["model"]),
        model_overrides=dict(worker.get("model_overrides") or {}),
        timeout_seconds=30,
        max_iterations_per_run=1,
        api_key="test-key",
    )


@pytest.mark.parametrize("prefix", B4_CODING)
def test_b4_coding_roles_use_mini(prefix: str) -> None:
    adapter = _adapter_from_overlay(B4_CONFIG)
    assert adapter._resolve_model({"task_summary": f"{prefix} do work"}) == "gpt-5.4-mini"


@pytest.mark.parametrize("prefix", B4_REASONING)
def test_b4_reasoning_roles_use_codex(prefix: str) -> None:
    adapter = _adapter_from_overlay(B4_CONFIG)
    assert adapter._resolve_model({"task_summary": f"{prefix} analyze"}) == "gpt-5.3-codex"


@pytest.mark.parametrize("prefix", B4_PROCEDURE)
def test_b4_procedure_roles_have_no_override(prefix: str) -> None:
    """Procedure roles execute host-side; any residual agent path keeps base mini."""
    adapter = _adapter_from_overlay(B4_CONFIG)
    assert adapter._resolve_model({"task_summary": f"{prefix} verify"}) == "gpt-5.4-mini"
    assert prefix not in adapter._model_overrides


@pytest.mark.parametrize("prefix", B3_DIRECT_COMPACT)
def test_b3_direct_compact_role_matches_b4_model(prefix: str) -> None:
    b3 = _adapter_from_overlay(B3_CONFIG)
    b4 = _adapter_from_overlay(B4_CONFIG)
    task = {"task_summary": f"{prefix} execute the assigned role"}
    assert b3._resolve_model(task) == b4._resolve_model(task)
