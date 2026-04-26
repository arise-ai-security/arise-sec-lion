"""Validate an experiments study manifest before any run executes.

Catches: unknown groups, unknown runners, missing config files, cell name /
group letter mismatches, missing required fields.
Runs at the top of `run_matrix.py` (PR 6) and as a pre-commit hook.
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import TYPE_CHECKING, Any

import yaml

from config._paths import get_repo_root


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)
_GROUP_KEY_RE = re.compile(r"^[A-Z]$")


def _load_groups(repo_root: Path) -> dict[str, str]:
    path = repo_root / "experiments" / "shared" / "groups.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"groups registry missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping of group letter -> label")
    bad = [k for k in data if not _GROUP_KEY_RE.fullmatch(str(k))]
    if bad:
        raise ValueError(f"{path}: group keys must be single uppercase letters A-Z; got: {bad}")
    return {str(k): str(v) for k, v in data.items()}


def validate_manifest(manifest: dict[str, Any], *, repo_root: Path) -> None:  # noqa: PLR0912
    """Raise ValueError listing every problem; success is silent."""
    from experiments.shared import runners

    groups = _load_groups(repo_root)
    known_runners = set(runners.all_ids())
    errors: list[str] = []

    cells = manifest.get("cells") or {}
    if not isinstance(cells, dict):
        errors.append(f"manifest.cells must be a mapping (got {type(cells).__name__})")
        cells = {}
    if not cells:
        errors.append("manifest.cells is empty")

    study_id = manifest.get("study_id")
    if not study_id:
        errors.append("manifest.study_id missing")

    for cell_name, spec in cells.items():
        if not isinstance(spec, dict):
            errors.append(f"cells.{cell_name}: must be a mapping (got {type(spec).__name__})")
            continue
        for required in ("group", "runner", "config"):
            if required not in spec:
                errors.append(f"cells.{cell_name}: missing {required!r}")

        group = spec.get("group")
        if group is not None and group not in groups:
            errors.append(
                f"cells.{cell_name}.group = {group!r} not in groups.yaml; "
                f"known groups: {sorted(groups)}"
            )
        if group is not None and group in groups and not cell_name.startswith(group):
            errors.append(
                f"cells.{cell_name}: name must start with group letter "
                f"{group!r} (got {cell_name!r})"
            )

        runner_id = spec.get("runner")
        if runner_id is not None and runner_id not in known_runners:
            errors.append(
                f"cells.{cell_name}.runner = {runner_id!r} not registered; "
                f"known runners: {sorted(known_runners)}"
            )

        config_path = spec.get("config")
        if config_path is not None and not isinstance(config_path, str):
            errors.append(
                f"cells.{cell_name}.config must be a string path (got {type(config_path).__name__})"
            )
            continue
        if config_path and study_id:
            full = repo_root / "experiments" / study_id / config_path
            if not full.is_file():
                errors.append(f"cells.{cell_name}.config not found: {full}")

    if errors:
        raise ValueError("manifest validation failed:\n  - " + "\n  - ".join(errors))


def _validate_study(study_id: str, *, repo_root: Path) -> int:
    """Validate one study; return process exit code (0 on success)."""
    manifest_path = repo_root / "experiments" / study_id / "manifest.yaml"
    if not manifest_path.is_file():
        logger.error("manifest not found: %s", manifest_path)
        return 1
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    try:
        validate_manifest(manifest, repo_root=repo_root)
    except ValueError as exc:
        logger.error("[%s] %s", study_id, exc)
        return 1
    logger.info("[%s] manifest OK", study_id)
    return 0


def _discover_studies(repo_root: Path) -> list[str]:
    studies_root = repo_root / "experiments"
    out: list[str] = []
    for child in sorted(studies_root.iterdir()):
        if not child.is_dir() or child.name in {"shared", "__pycache__"}:
            continue
        if (child / "manifest.yaml").is_file():
            out.append(child.name)
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="validate_manifest")
    parser.add_argument(
        "--study",
        help="Study ID to validate. If omitted, validates every study under experiments/.",
    )
    args = parser.parse_args(argv)

    repo_root = get_repo_root()
    studies = [args.study] if args.study else _discover_studies(repo_root)
    if not studies:
        logger.error("no studies found under %s/experiments/", repo_root)
        return 1

    rc = 0
    for sid in studies:
        rc |= _validate_study(sid, repo_root=repo_root)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
