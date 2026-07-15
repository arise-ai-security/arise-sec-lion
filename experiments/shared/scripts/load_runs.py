"""Walk runs pool roots and yield `run_manifest.json` records matching a filter.

A "pool root" is any directory that holds `<run_id>/run_manifest.json`
subdirectories. By default this loader walks ``<repo_root>/runs`` and the
configured ``settings.output.directory`` (defaulting to ``./output`` under
the repo root). Tests can override the search by passing explicit
``pool_roots`` and bypass the Settings dependency.

Returns plain ``list[dict]`` so study-specific scripts can adopt pandas
(or not) as they see fit.

Usage::

    from experiments.shared.scripts.load_runs import load_runs

    rows = load_runs(
        study_id="2026-04-22-demo",
        cells=["A1", "B2"],
    )
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from experiments.shared.scripts._paths import get_repo_root


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


logger = logging.getLogger(__name__)


def default_pool_roots() -> list[Path]:
    """Return the pool roots scanned when no explicit roots are supplied.

    Walks ``<repo_root>/runs`` plus the configured ``settings.output.directory``
    (loaded lazily so tests that do not need full Settings are not forced to
    instantiate it).
    """
    repo_root = get_repo_root()
    roots: list[Path] = [repo_root / "runs"]

    output_dir = _settings_output_directory()
    if output_dir is not None and output_dir not in roots:
        roots.append(output_dir)
    return roots


def _settings_output_directory() -> Path | None:
    """Resolve ``settings.output.directory`` to an absolute Path, or None.

    Reads the raw merged YAML rather than the full pydantic ``Settings``
    model. ``Settings.load()`` requires experiment model names that the
    base ``config.yaml`` intentionally omits (see
    ``test_base_config_without_models_fails``); validating it just to read
    ``output.directory`` would emit a misleading "model missing" warning
    on every cell-less call. Any failure (missing files, malformed YAML)
    downgrades to ``None`` so the harness still finds runs under ``runs/``.
    """
    try:
        from config.settings import _load_yaml_hierarchy

        merged = _load_yaml_hierarchy()
    except (ImportError, FileNotFoundError, yaml.YAMLError) as exc:
        logger.warning("could not load YAML to resolve output.directory: %s", exc)
        return None

    raw = (merged.get("output") or {}).get("directory")
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (get_repo_root() / candidate).resolve()
    return candidate


def _resolve_pool_roots(
    *,
    pool_roots: Iterable[Path] | None,
    output_directory: str | Path | None,
) -> list[Path]:
    """Reconcile the explicit overrides into a deduplicated list of roots.

    ``pool_roots`` (when given) wins outright — tests use it to point the
    walker at a tmp dir without touching Settings. ``output_directory``
    is the legacy single-root override; we keep it for back-compat. Falling
    through both yields the default pool roots.
    """
    if pool_roots is not None:
        return list(dict.fromkeys(Path(p) for p in pool_roots))
    if output_directory is not None:
        return [Path(output_directory)]
    return default_pool_roots()


def _iter_manifest_paths_for_root(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return

    for manifest in root.glob("*/run_manifest.json"):
        yield manifest


def iter_run_manifests(
    pool_roots: Iterable[Path] | None = None,
    *,
    output_directory: str | Path | None = None,
) -> Iterator[Path]:
    """Yield every `run_manifest.json` under the resolved pool roots.

    Public so other scripts (notably ``collect.build_enrollment_lock``)
    don't need to redefine the walker.
    """
    resolved = _resolve_pool_roots(pool_roots=pool_roots, output_directory=output_directory)
    for root in resolved:
        yield from _iter_manifest_paths_for_root(root)


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
    output_directory: str | Path | None = None,
    pool_roots: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    """Load every matching `run_manifest.json` as a list of dicts.

    Walks every pool root (see ``default_pool_roots``) by default; pass
    ``pool_roots`` to override (tests do this) or ``output_directory`` to
    keep the single-root legacy behavior.
    """
    # Collapse any one-shot iterable filters (e.g. generators) into a frozen
    # set so they survive the per-record matching loop.
    cells_set = frozenset(cells) if cells is not None else None
    tasks_set = frozenset(tasks) if tasks is not None else None

    records: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for manifest_path in iter_run_manifests(
        pool_roots=pool_roots,
        output_directory=output_directory,
    ):
        try:
            raw = manifest_path.read_text(encoding="utf-8")
            record = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping unreadable manifest %s: %s", manifest_path, exc)
            continue
        if not isinstance(record, dict):
            logger.warning("skipping non-object manifest: %s", manifest_path)
            continue
        run_id = record.get("run_id")
        if isinstance(run_id, str) and run_id in seen_run_ids:
            # Same run_id surfacing in two pool roots means migration
            # left a duplicate; collect.build_enrollment_lock raises in
            # that case. load_runs is the read-only path, so we just keep
            # the first record we saw and drop the dup.
            continue
        if isinstance(run_id, str):
            seen_run_ids.add(run_id)
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
