"""Overlay resolver: load ``extends:`` + ``overrides:`` YAML files, merging
into the base recursively. Returns a dict ready for
``Settings._build_from_config``.

A cell config like::

    extends: config/config.yaml
    overrides:
      orchestration.mode: flat
      worker.tool: claude_code

resolves to ``config/config.yaml`` with the dotted keys merged in.
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def resolve_overlay(path: Path, *, repo_root: Path) -> dict[str, Any]:
    """Load YAML at ``path``; if it has ``extends:`` + ``overrides:``, merge
    recursively against the resolved base.

    Args:
        path: YAML file to load.
        repo_root: Root directory used to resolve relative ``extends:`` paths.

    Returns:
        Materialized configuration dict.

    Raises:
        ValueError: Top-level YAML is not a mapping, or only one of
            ``extends:``/``overrides:`` is present.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    has_extends = "extends" in raw
    has_overrides = "overrides" in raw

    if has_extends ^ has_overrides:
        raise ValueError(
            f"{path}: 'extends' and 'overrides' must appear together "
            f"(got extends={has_extends}, overrides={has_overrides})"
        )

    if not has_extends:
        return raw

    base_rel = raw["extends"]
    base_path = Path(base_rel)
    if not base_path.is_absolute():
        base_path = repo_root / base_path

    base = resolve_overlay(base_path, repo_root=repo_root)
    overrides = _flatten_dotted(raw["overrides"] or {})
    return _deep_merge(base, overrides)


def _flatten_dotted(d: dict[str, Any]) -> dict[str, Any]:
    """Convert ``{"a.b.c": 1}`` to ``{"a": {"b": {"c": 1}}}``.

    Mixed forms are supported: ``{"a.b": {"c": 1}}`` and ``{"a": {"b.c": 1}}``
    both produce ``{"a": {"b": {"c": 1}}}``.
    """
    result: dict[str, Any] = {}
    for key, value in d.items():
        expanded_value = _flatten_dotted(value) if isinstance(value, dict) else value
        if "." in key:
            parts = key.split(".")
            cursor: dict[str, Any] = result
            for part in parts[:-1]:
                existing = cursor.get(part)
                if existing is None:
                    next_cursor: dict[str, Any] = {}
                    cursor[part] = next_cursor
                    cursor = next_cursor
                elif isinstance(existing, dict):
                    cursor = existing
                else:
                    raise ValueError(f"Cannot expand dotted key {key!r}: collision at {part!r}")
            leaf = parts[-1]
            existing_leaf = cursor.get(leaf)
            if isinstance(existing_leaf, dict) and isinstance(expanded_value, dict):
                cursor[leaf] = _deep_merge(existing_leaf, expanded_value)
            else:
                cursor[leaf] = expanded_value
        else:
            existing_top = result.get(key)
            if isinstance(existing_top, dict) and isinstance(expanded_value, dict):
                result[key] = _deep_merge(existing_top, expanded_value)
            else:
                result[key] = expanded_value
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge; override wins on scalars; lists are REPLACED (not concatenated)."""
    out = deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m config.overlay",
        description="Resolve a YAML overlay (extends:/overrides:) and print the merged dict.",
    )
    parser.add_argument(
        "--resolve",
        required=True,
        type=Path,
        help="Path to the overlay YAML to resolve.",
    )
    args = parser.parse_args(argv)

    from config._paths import get_repo_root

    merged = resolve_overlay(args.resolve, repo_root=get_repo_root())
    yaml.safe_dump(merged, sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
