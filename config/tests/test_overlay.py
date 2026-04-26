"""Tests for the YAML overlay resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from config.overlay import resolve_overlay


if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_overlay_loads_extends_and_overrides(tmp_path: Path) -> None:
    """resolve_overlay merges dotted overrides into the base config."""

    # Given: a base file and an overlay that extends it with a dotted override.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"a": {"b": 1, "c": 2}, "d": 3})
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(
        overlay,
        {"extends": "base.yaml", "overrides": {"a.b": 10, "d": 30}},
    )

    # When: the overlay is resolved against the tmp_path repo root.
    merged = resolve_overlay(overlay, repo_root=tmp_path)

    # Then: the override values replace base values; untouched fields persist.
    assert merged == {"a": {"b": 10, "c": 2}, "d": 30}


def test_overlay_dotted_keys_flatten(tmp_path: Path) -> None:
    """Dotted override keys expand into nested dicts."""

    # Given: a base with nested fields and an overlay using deeply dotted keys.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"orchestration": {"mode": "hierarchical", "topology": {"max_depth": 3}}})
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(
        overlay,
        {
            "extends": "base.yaml",
            "overrides": {"orchestration.mode": "flat", "orchestration.topology.max_depth": 0},
        },
    )

    # When: the overlay is resolved.
    merged = resolve_overlay(overlay, repo_root=tmp_path)

    # Then: the dotted keys are expanded and merged correctly.
    assert merged["orchestration"]["mode"] == "flat"
    assert merged["orchestration"]["topology"]["max_depth"] == 0


def test_overlay_nested_keys_in_overrides_merge_correctly(tmp_path: Path) -> None:
    """Mixed nested-and-dotted overrides merge into a single tree."""

    # Given: base with multi-key nested section, overlay using mixed override forms.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"worker": {"tool": "openhands", "timeout": 600}})
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(
        overlay,
        {
            "extends": "base.yaml",
            "overrides": {
                "worker": {"timeout": 1000},
                "worker.tool_params": {"openhands": {"image": "x:y"}},
            },
        },
    )

    # When: the overlay is resolved.
    merged = resolve_overlay(overlay, repo_root=tmp_path)

    # Then: nested and dotted overrides combine without losing existing fields.
    assert merged["worker"]["tool"] == "openhands"
    assert merged["worker"]["timeout"] == 1000
    assert merged["worker"]["tool_params"]["openhands"]["image"] == "x:y"


def test_overlay_lists_are_replaced_not_concatenated(tmp_path: Path) -> None:
    """Lists in overrides REPLACE the base list (no concat)."""

    # Given: base with a list field; overlay specifies a shorter replacement.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"tools": ["a", "b", "c"]})
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(overlay, {"extends": "base.yaml", "overrides": {"tools": ["only"]}})

    # When: the overlay is resolved.
    merged = resolve_overlay(overlay, repo_root=tmp_path)

    # Then: the override list replaces the base list.
    assert merged["tools"] == ["only"]


def test_overlay_extends_without_overrides_raises(tmp_path: Path) -> None:
    """extends without overrides is malformed."""

    # Given: an overlay with extends but no overrides key.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"a": 1})
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(overlay, {"extends": "base.yaml"})

    # When/Then: resolve_overlay raises ValueError.
    with pytest.raises(ValueError, match="must appear together"):
        resolve_overlay(overlay, repo_root=tmp_path)


def test_overlay_overrides_without_extends_raises(tmp_path: Path) -> None:
    """overrides without extends is malformed."""

    # Given: an overlay with overrides but no extends key.
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(overlay, {"overrides": {"a": 1}})

    # When/Then: resolve_overlay raises ValueError.
    with pytest.raises(ValueError, match="must appear together"):
        resolve_overlay(overlay, repo_root=tmp_path)


def test_overlay_chain_extends_overlay(tmp_path: Path) -> None:
    """An overlay can extend another overlay (recursive resolution)."""

    # Given: base -> mid -> top chain of three YAMLs.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"a": 1, "b": 2, "c": 3})
    mid = tmp_path / "mid.yaml"
    _write_yaml(mid, {"extends": "base.yaml", "overrides": {"b": 20}})
    top = tmp_path / "top.yaml"
    _write_yaml(top, {"extends": "mid.yaml", "overrides": {"c": 300}})

    # When: the top overlay is resolved.
    merged = resolve_overlay(top, repo_root=tmp_path)

    # Then: each layer's overrides apply on top of the previous.
    assert merged == {"a": 1, "b": 20, "c": 300}


def test_overlay_relative_extends_resolves_from_repo_root(tmp_path: Path) -> None:
    """A repo-root-relative extends path is resolved against repo_root, not the overlay's dir."""

    # Given: base in repo_root, overlay in a subdirectory referencing it relatively.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {"a": 1})
    sub = tmp_path / "experiments" / "study"
    sub.mkdir(parents=True)
    overlay = sub / "overlay.yaml"
    _write_yaml(overlay, {"extends": "base.yaml", "overrides": {"a": 99}})

    # When: the overlay is resolved against the repo root (tmp_path).
    merged = resolve_overlay(overlay, repo_root=tmp_path)

    # Then: the base is found relative to repo_root.
    assert merged == {"a": 99}


def test_overlay_top_level_must_be_mapping_raises(tmp_path: Path) -> None:
    """A YAML whose top-level is a list is rejected."""

    # Given: a YAML file whose top-level is a list, not a mapping.
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("- item1\n- item2\n", encoding="utf-8")

    # When/Then: resolve_overlay raises ValueError.
    with pytest.raises(ValueError, match="top-level YAML must be a mapping"):
        resolve_overlay(overlay, repo_root=tmp_path)


def test_overlay_empty_yaml_returns_empty_dict(tmp_path: Path) -> None:
    """An empty YAML resolves to an empty dict (no extends/overrides)."""

    # Given: an empty YAML file.
    overlay = tmp_path / "empty.yaml"
    overlay.write_text("", encoding="utf-8")

    # When: it is resolved.
    merged = resolve_overlay(overlay, repo_root=tmp_path)

    # Then: the result is an empty dict.
    assert merged == {}


def test_overlay_dotted_collision_raises(tmp_path: Path) -> None:
    """Dotted key whose prefix collides with a non-dict value raises."""

    # Given: an overlay where a.b is a scalar but a.b.c is also written.
    base = tmp_path / "base.yaml"
    _write_yaml(base, {})
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(
        overlay,
        {"extends": "base.yaml", "overrides": {"a.b": 1, "a.b.c": 2}},
    )

    # When/Then: resolve_overlay raises ValueError.
    with pytest.raises(ValueError, match="collision"):
        resolve_overlay(overlay, repo_root=tmp_path)
