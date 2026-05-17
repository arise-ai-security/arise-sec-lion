"""Authoritative study inputs for DB-first experiment analysis.

This module deliberately reads only study design files and the enrollment
lockfile. It does not inspect ``run_manifest.json`` or projected
``events.jsonl`` files; those are non-authoritative for DB-first analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from experiments.shared.scripts._paths import get_repo_root


@dataclass(frozen=True)
class StudyCell:
    """One cell declared by ``manifest.yaml``."""

    name: str
    group: str
    runner: str
    config_path: Path


@dataclass(frozen=True)
class CohortEntry:
    """One run enrolled in a study lockfile."""

    run_id: str
    cell: str
    task: str
    replicate: int


@dataclass(frozen=True)
class StudyDefinition:
    """DB-first study metadata loaded from authoritative YAML files."""

    study_id: str
    study_dir: Path
    manifest_path: Path
    dataset_path: Path
    enrollment_path: Path
    target_tasks: tuple[str, ...]
    cells: dict[str, StudyCell]
    headline_cells: tuple[str, ...]
    cohort: tuple[CohortEntry, ...]

    @property
    def input_paths(self) -> tuple[Path, ...]:
        """Files that define the cohort and cell mapping.

        DB events are intentionally not represented as a filesystem input.
        Generated tables include ``source_authority=db_events`` to make that
        source explicit.
        """
        config_paths = tuple(cell.config_path for cell in self.cells.values())
        return (
            self.manifest_path,
            self.dataset_path,
            *config_paths,
            self.enrollment_path,
        )


def load_study_definition(study: str | Path) -> StudyDefinition:
    """Load a study's DB-first design inputs.

    ``study`` may be a study id such as ``a12-batch-autogen`` or a path to an
    ``experiments/<study>`` directory.
    """
    study_dir = _resolve_study_dir(study)
    manifest_path = study_dir / "manifest.yaml"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing study manifest: {manifest_path}")

    manifest = _read_yaml_mapping(manifest_path)
    study_id = _required_str(manifest, "study_id", manifest_path)

    dataset_rel = _required_str(manifest, "dataset", manifest_path)
    dataset_path = (study_dir / dataset_rel).resolve()
    dataset = _read_yaml_mapping(dataset_path)
    target_tasks = _string_tuple(dataset.get("default_cves"), dataset_path, "default_cves")

    cells = _load_cells(study_dir, manifest)
    headline_cells = _string_tuple(
        manifest.get("headline_cells") or tuple(cells),
        manifest_path,
        "headline_cells",
    )

    enrollment_path = study_dir / "reports" / "enrollment.lock.yaml"
    enrollment = _read_yaml_mapping(enrollment_path)
    cohort = _load_cohort(enrollment_path, enrollment)

    return StudyDefinition(
        study_id=study_id,
        study_dir=study_dir,
        manifest_path=manifest_path,
        dataset_path=dataset_path,
        enrollment_path=enrollment_path,
        target_tasks=target_tasks,
        cells=cells,
        headline_cells=headline_cells,
        cohort=cohort,
    )


def first_config_path(study: StudyDefinition) -> Path:
    """Return the first configured cell path for DB connection settings."""
    for cell_name in study.headline_cells:
        cell = study.cells.get(cell_name)
        if cell is not None:
            return cell.config_path
    try:
        return next(iter(study.cells.values())).config_path
    except StopIteration as exc:
        raise ValueError(f"study {study.study_id!r} declares no cells") from exc


def _resolve_study_dir(study: str | Path) -> Path:
    raw = Path(study)
    if raw.is_dir():
        return raw.resolve()
    return (get_repo_root() / "experiments" / str(study)).resolve()


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return parsed


def _required_str(mapping: dict[str, Any], key: str, path: Path) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: {key!r} must be a non-empty string")
    return value


def _string_tuple(value: Any, path: Path, key: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{path}: {key!r} must be a list of strings")
    result = tuple(item for item in value if isinstance(item, str) and item)
    if len(result) != len(value):
        raise ValueError(f"{path}: {key!r} must contain only non-empty strings")
    return result


def _load_cells(study_dir: Path, manifest: dict[str, Any]) -> dict[str, StudyCell]:
    raw_cells = manifest.get("cells")
    if not isinstance(raw_cells, dict) or not raw_cells:
        raise ValueError(f"{study_dir / 'manifest.yaml'}: 'cells' must be a non-empty mapping")

    cells: dict[str, StudyCell] = {}
    for name, raw in raw_cells.items():
        if not isinstance(name, str) or not name:
            raise ValueError("manifest cells must be keyed by non-empty string names")
        if not isinstance(raw, dict):
            raise ValueError(f"cells.{name}: expected a mapping")
        config_rel = _required_str(raw, "config", study_dir / "manifest.yaml")
        config_path = (study_dir / config_rel).resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"cells.{name}.config not found: {config_path}")
        cells[name] = StudyCell(
            name=name,
            group=_required_str(raw, "group", study_dir / "manifest.yaml"),
            runner=_required_str(raw, "runner", study_dir / "manifest.yaml"),
            config_path=config_path,
        )
    return cells


def _load_cohort(path: Path, enrollment: dict[str, Any]) -> tuple[CohortEntry, ...]:
    raw_rows = enrollment.get("enrollment")
    if not isinstance(raw_rows, list):
        raise ValueError(f"{path}: 'enrollment' must be a list")

    rows: list[CohortEntry] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: enrollment[{idx}] must be a mapping")
        run_id = _required_str(raw, "run_id", path)
        if run_id in seen:
            raise ValueError(f"{path}: duplicate run_id in enrollment lock: {run_id}")
        seen.add(run_id)
        rows.append(
            CohortEntry(
                run_id=run_id,
                cell=_required_str(raw, "cell", path),
                task=_required_str(raw, "task", path),
                replicate=int(raw.get("replicate", 0)),
            )
        )
    return tuple(rows)
