"""Boundary-clean CVE oracle resolution for offline judging (raw projection).

The evaluation package must NOT import ``plugins/security`` (composition-root
boundary). This module reads the *same* host-side dataset JSON the harness
resolved to launch a run — by stdlib ``json`` only — and projects it verbatim
into a plugin-free :class:`CveOracle`. It performs NO semantic derivation: every
field is copied as-is. All semantic judgement (expected error class, crash site,
patch root-cause) is deferred to the LLM judges, which receive these raw bytes.

Run → oracle linkage: ``run_manifest.json`` carries the task slug under ``task``
(e.g. ``"gpac.cve-2023-46001"``); ``instance_id`` is ``null`` on real runs, so we
key on ``task``. The slug resolves to a dataset JSON under one of the on-disk
oracle roots (the dataset ``source.paths`` the harness already validated).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from experiments.shared.evaluation.models import CveOracle


logger = logging.getLogger(__name__)

# experiments/shared/evaluation/oracle.py → repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]

# On-disk roots holding per-instance CVE JSON (same schema as the harness
# dataset source.paths). The eval layer reads these directly; it never imports
# the security plugin that also hydrates them.
ORACLE_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "deployment" / "cve-instances",
    REPO_ROOT / "plugins" / "security" / "tests" / "fixtures",
)


def oracle_from_dict(data: dict[str, Any], *, include_gold_patch: bool = False) -> CveOracle:
    """Project a dataset JSON dict into a :class:`CveOracle` (raw, no plugin import).

    A trivial verbatim field copy — no semantic derivation. ``include_gold_patch``
    controls whether the host-side secret ``patch`` is carried for the
    patch-correctness judge. It defaults to ``False`` so the gold patch is opt-in
    and never accidentally threaded.
    """
    gold = str(data.get("patch", "")) if include_gold_patch else None
    return CveOracle(
        instance_id=str(data.get("instance_id", "")),
        sanitizer=str(data.get("sanitizer", "")),
        sanitizer_report=str(data.get("sanitizer_report", "")),
        bug_report=str(data.get("bug_report", "")),
        bug_description=str(data.get("bug_description", "")),
        base_commit=str(data.get("base_commit", "")),
        gold_patch=gold or None,
    )


def oracle_from_file(path: Path | str, *, include_gold_patch: bool = False) -> CveOracle:
    """Load a dataset JSON file and project it (stdlib json; no plugin import)."""
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"CVE oracle JSON at {path} is not an object")
    return oracle_from_dict(data, include_gold_patch=include_gold_patch)


def _slug_matches_stem(slug: str, stem: str) -> bool:
    return slug.replace(".", "-").lower() == stem.replace(".", "-").lower()


def resolve_oracle_path(task_slug: str, *, roots: tuple[Path, ...] = ORACLE_ROOTS) -> Path | None:
    """Find the dataset JSON for a task slug across the on-disk oracle roots.

    Matches first on filename stem (dot/dash tolerant, e.g. ``gpac.cve-2023-5586``
    ↔ ``gpac-cve-2023-5586.json``), then on the ``instance_id`` recorded inside the
    file. Returns ``None`` when no instance matches.
    """
    if not task_slug:
        return None
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("*.json")):
            if _slug_matches_stem(task_slug, candidate.stem):
                return candidate
    # Stem miss: fall back to the recorded instance_id inside each file.
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("*.json")):
            try:
                with candidate.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and _slug_matches_stem(
                task_slug, str(data.get("instance_id", ""))
            ):
                return candidate
    return None


def build_oracle_for_manifest(
    manifest: dict[str, Any],
    *,
    roots: tuple[Path, ...] = ORACLE_ROOTS,
    include_gold_patch: bool = False,
) -> CveOracle | None:
    """Resolve a :class:`CveOracle` from a run manifest, or ``None`` if unavailable.

    Keys on ``manifest['task']`` (the slug; ``instance_id`` is ``null`` on real
    runs). Logs a warning and returns ``None`` when the slug is absent or no
    matching dataset JSON is found, so callers degrade to mechanical-only judging.
    """
    task_slug = str(manifest.get("task", "")).strip()
    if not task_slug:
        logger.warning("Manifest has no 'task' slug; CVE oracle unavailable")
        return None
    path = resolve_oracle_path(task_slug, roots=roots)
    if path is None:
        logger.warning("No CVE oracle JSON found for task %r under %s", task_slug, roots)
        return None
    return oracle_from_file(path, include_gold_patch=include_gold_patch)
