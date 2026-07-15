"""Prevent experiment identities from becoming runtime behavior switches."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
RUNTIME_PATHS = (
    "main.py",
    "bootstrap",
    "config",
    "core",
    "infrastructure",
    "plugins",
    "presentation",
    "prompts",
    "query",
    "scripts",
)
RUNTIME_SUFFIXES = {
    ".css",
    ".j2",
    ".js",
    ".jsx",
    ".json",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
IGNORED_PATH_PARTS = {"__pycache__", "dist", "node_modules"}
EXPERIMENT_ID = re.compile(
    r"(?<![A-Za-z0-9_\\])(?i:n1|b3|b4)(?![A-Za-z0-9_])"
    r"|(?i:n1-secbench-full|b3-direct-compact|b4-boss-manager-worker"
    r"|b4-adaptive|b3-role|role[_-]?fused|adaptive_execution|_adaptive_treatments)",
)
EXPERIMENT_IMPORT = re.compile(
    r"^\s*(?:from\s+experiments\b|import\s+experiments\b)",
    re.MULTILINE,
)


def _runtime_source_files() -> list[Path]:
    files: list[Path] = []
    for relative_path in RUNTIME_PATHS:
        path = REPO_ROOT / relative_path
        if path.is_file():
            files.append(path)
        else:
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and candidate.suffix in RUNTIME_SUFFIXES
                and not IGNORED_PATH_PARTS.intersection(candidate.parts)
            )
    return files


def test_runtime_source_contains_no_experiment_identifiers() -> None:
    violations = [
        path.relative_to(REPO_ROOT)
        for path in _runtime_source_files()
        if EXPERIMENT_ID.search(str(path.relative_to(REPO_ROOT)))
        or EXPERIMENT_ID.search(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def test_runtime_source_does_not_import_experiment_packages() -> None:
    violations = [
        path.relative_to(REPO_ROOT)
        for path in _runtime_source_files()
        if EXPERIMENT_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert violations == []
