"""Walk the runs/ pool and yield `run_manifest.json` records matching a filter.

Returns plain ``list[dict]`` so study-specific scripts can adopt pandas
(or not) as they see fit. Legacy runs namespaced under `runs/_legacy/` are
included unless ``include_legacy=False``.

Usage::

    from experiments.shared.scripts.load_runs import load_runs

    rows = load_runs(
        study_id="2026-04-22-demo",
        cells=["A1", "B2"],
    )
    # → list[dict] shaped like `run_manifest.json`
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from experiments.shared.scripts._paths import get_repo_root


if TYPE_CHECKING:
    from collections.abc import Iterable


logger = logging.getLogger(__name__)


def _runs_pool_root(output_directory: str | Path | None = None) -> Path:
    if output_directory is None:
        return get_repo_root() / "runs"
    return Path(output_directory)


def _iter_manifest_paths(runs_pool: Path, include_legacy: bool) -> Iterable[Path]:
    """Yield every `run_manifest.json` beneath the pool."""
    if not runs_pool.is_dir():
        return

    for manifest in runs_pool.glob("*/run_manifest.json"):
        yield manifest

    if include_legacy:
        legacy_root = runs_pool / "_legacy"
        if legacy_root.is_dir():
            for manifest in legacy_root.glob("*/run_manifest.json"):
                yield manifest


def _matches(record: dict[str, Any], **filters: Any) -> bool:
    """Return True iff record matches every non-None filter.

    ``cells`` and ``tasks`` are expected to be frozensets by the time we get
    here (normalized in ``load_runs``) so they survive repeated lookups.
    Scalar filters match equality.
    """
    cells = filters.get("cells")
    if cells is not None and record.get("cell") not in cells:
        return False

    tasks = filters.get("tasks")
    if tasks is not None and record.get("task") not in tasks:
        return False

    for key in ("study_id", "exit_status", "kind"):
        expected = filters.get(key)
        if expected is not None and record.get(key) != expected:
            return False
    return True


def load_runs(
    *,
    study_id: str | None = None,
    cells: Iterable[str] | None = None,
    tasks: Iterable[str] | None = None,
    exit_status: str | None = None,
    kind: str | None = None,
    include_legacy: bool = True,
    output_directory: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Load every matching `run_manifest.json` as a list of dicts."""
    pool = _runs_pool_root(output_directory)

    # Collapse any one-shot iterable filters (e.g. generators) into a frozen
    # set so they survive the per-record matching loop.
    cells_set = frozenset(cells) if cells is not None else None
    tasks_set = frozenset(tasks) if tasks is not None else None

    records: list[dict[str, Any]] = []
    for manifest_path in _iter_manifest_paths(pool, include_legacy=include_legacy):
        try:
            raw = manifest_path.read_text(encoding="utf-8")
            record = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping unreadable manifest %s: %s", manifest_path, exc)
            continue
        if not isinstance(record, dict):
            logger.warning("skipping non-object manifest: %s", manifest_path)
            continue
        if _matches(
            record,
            cells=cells_set,
            tasks=tasks_set,
            study_id=study_id,
            exit_status=exit_status,
            kind=kind,
        ):
            records.append(record)
    return records
