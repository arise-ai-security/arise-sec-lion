"""Experiment-local checks for B3 direct-compact parity with B4."""

from __future__ import annotations

from pathlib import Path

import yaml

from config.overlay import resolve_overlay


REPO_ROOT = Path(__file__).resolve().parents[4]
B3_CONFIG = REPO_ROOT / "experiments/b3-direct-compact/configs/B3-direct-compact.yaml"
B3_MANIFEST = REPO_ROOT / "experiments/b3-direct-compact/manifest.yaml"
B4_CONFIG = REPO_ROOT / "experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml"

def test_b3_differs_from_b4_only_by_manager_topology() -> None:
    b3 = resolve_overlay(B3_CONFIG, repo_root=REPO_ROOT)
    b4 = resolve_overlay(B4_CONFIG, repo_root=REPO_ROOT)

    b3_treatment = b3["orchestration"].pop("treatment_version")
    b4_treatment = b4["orchestration"].pop("treatment_version")
    b3_manager_layer = b3["orchestration"]["topology"].pop("include_manager_layer")
    b4_manager_layer = b4["orchestration"]["topology"].pop(
        "include_manager_layer", True
    )

    assert b3_treatment == "b3-direct-compact-v1"
    assert b4_treatment == "b4-adaptive-manager-v2"
    assert b3_manager_layer is False
    assert b4_manager_layer is True
    assert b3 == b4


def test_b3_manifest_points_to_renamed_direct_compact_config() -> None:
    manifest = yaml.safe_load(B3_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["study_id"] == "b3-direct-compact"
    assert manifest["cells"]["B3"]["config"] == "configs/B3-direct-compact.yaml"
