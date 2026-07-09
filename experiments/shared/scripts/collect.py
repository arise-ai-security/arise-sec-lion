"""Build the per-study enrollment lockfile from the runs pool.

The study's ``manifest.yaml`` is design-only (cells, hypothesis, dataset).
The enrollment roster — which (cell, task, replicate) cells were sampled
by which `run_id` — is a derived artifact regenerated from
``run_manifest.json`` files in every pool root and committed alongside
the report as ``experiments/<study>/reports/enrollment.lock.yaml``.

Usage::

    python -m experiments.shared.scripts.collect --study <study-id>

The lockfile is written via ``write_binary`` so it carries the same
``.generated.json`` provenance as every other artifact in the reports tree.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from experiments.shared.scripts._paths import get_repo_root, to_repo_relative
from experiments.shared.scripts.load_runs import default_pool_roots, iter_run_manifests
from experiments.shared.scripts.write_report import write_binary


if TYPE_CHECKING:
    from collections.abc import Iterable


logger = logging.getLogger(__name__)


ENROLLMENT_LOCK_FILENAME = "enrollment.lock.yaml"


class DuplicateRunIdError(RuntimeError):
    """Two different run_manifest.json files claim the same run_id."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _study_manifest_path(study_id: str) -> Path:
    return get_repo_root() / "experiments" / study_id / "manifest.yaml"


def _git_sha_of(path: Path) -> str | None:
    """Return the current git blob sha for ``path``, or None if unavailable.

    ``git hash-object`` works even when the file isn't committed yet, so
    the lockfile's ``design_sha`` always reflects the on-disk manifest at
    the moment the lock was rendered.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — argv is fixed
            [git_bin, "hash-object", str(path)],
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout.strip() or None


def _replicate_of(record: dict[str, Any]) -> int:
    """Pull the replicate index from a run manifest."""
    return int(record.get("replicate", 0))


def _load_enrolled_record(
    manifest_path: Path,
    *,
    study_id: str,
    enrolled_index: dict[str, dict[str, Any]],
) -> None:
    """Read one ``run_manifest.json`` and merge it into ``enrolled_index``.

    Raises ``DuplicateRunIdError`` if a different run_manifest already in
    the index claims the same run_id with conflicting (cell, task,
    replicate) — that would silently corrupt the enrollment roster.
    """
    try:
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("skipping unreadable manifest %s: %s", manifest_path, exc)
        return

    if not isinstance(record, dict):
        logger.warning("skipping non-object manifest: %s", manifest_path)
        return

    if record.get("study_id") != study_id:
        return

    run_id = record.get("run_id")
    if not isinstance(run_id, str):
        logger.warning("skipping manifest with no run_id: %s", manifest_path)
        return

    cell = record.get("cell")
    task = record.get("task")
    if not isinstance(cell, str) or not isinstance(task, str):
        logger.warning(
            "skipping manifest %s: cell/task not stamped (run register_run first)",
            manifest_path,
        )
        return

    entry: dict[str, Any] = {
        "run_id": run_id,
        "cell": cell,
        "task": task,
        "replicate": _replicate_of(record),
    }
    # Optional provenance (PR 6): downstream drift checks can spot lockfile
    # or config drift between enrolled runs. Older manifests without these
    # fields enroll fine.
    uv_lock_sha = record.get("uv_lock_sha256")
    if isinstance(uv_lock_sha, str):
        entry["uv_lock_sha256"] = uv_lock_sha
    effective_config_path = record.get("effective_config_path")
    if isinstance(effective_config_path, str):
        entry["effective_config_path"] = effective_config_path

    existing = enrolled_index.get(run_id)
    if existing is not None and existing != entry:
        raise DuplicateRunIdError(
            f"run_id {run_id!r} appears in multiple pool roots with conflicting "
            f"enrollment fields: {existing!r} vs {entry!r}"
        )
    enrolled_index[run_id] = entry


def build_enrollment_lock(
    study_id: str,
    *,
    pool_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Build the enrollment lockfile dict for ``study_id`` from the runs pool.

    Walks every pool root (``runs/`` + ``settings.output.directory`` by
    default). Deduplicates on ``run_id`` and raises ``DuplicateRunIdError``
    if the same run_id surfaces with different (cell, task, replicate)
    values.

    The returned dict is serialization-ready: keys sort cleanly, values
    are JSON/YAML scalars only.
    """
    enrolled_index: dict[str, dict[str, Any]] = {}
    for manifest_path in iter_run_manifests(pool_roots=pool_roots):
        _load_enrolled_record(
            manifest_path,
            study_id=study_id,
            enrolled_index=enrolled_index,
        )

    enrolled = sorted(
        enrolled_index.values(),
        key=lambda r: (r["cell"], r["task"], r["replicate"], r["run_id"]),
    )

    return {
        "study_id": study_id,
        "rendered_at": _utcnow_iso(),
        "design_sha": _git_sha_of(_study_manifest_path(study_id)),
        "enrollment": enrolled,
    }


def _write_enrollment_lock(study_id: str, lock: dict[str, Any]) -> Path:
    """Atomically write ``enrollment.lock.yaml`` with ``write_binary`` provenance.

    Listing every contributing run_manifest.json as an "input" is brittle
    (paths and counts change every time a run is added). The manifest is
    the design contract that gates this output, so we treat it as the
    sole declared input and let ``design_sha`` inside the lockfile body
    record the manifest version actually consumed.
    """
    rel_study = f"experiments/{study_id}"
    manifest_rel = f"{rel_study}/manifest.yaml"
    output_rel = f"{rel_study}/reports/{ENROLLMENT_LOCK_FILENAME}"
    script_rel = "experiments/shared/scripts/collect.py"

    yaml_bytes = yaml.safe_dump(
        lock,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).encode("utf-8")

    return write_binary(
        path=output_rel,
        content=yaml_bytes,
        script=script_rel,
        inputs=[manifest_rel],
    )


def load_enrollment_lock(study_id: str) -> dict[str, Any]:
    """Read ``experiments/<study>/reports/enrollment.lock.yaml``.

    Raises ``FileNotFoundError`` if the lockfile is absent — callers
    (renderer, validator) want to surface that as "run collect first"
    rather than silently treating the study as empty.
    """
    path = get_repo_root() / "experiments" / study_id / "reports" / ENROLLMENT_LOCK_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"missing {to_repo_relative(path)}; run "
            "`python -m experiments.shared.scripts.collect --study "
            f"{study_id}` first"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"enrollment lockfile at {path} is not a YAML mapping")
    return data


def collect_study(
    study_id: str,
    *,
    pool_roots: Iterable[Path] | None = None,
) -> Path:
    if not _study_manifest_path(study_id).is_file():
        raise FileNotFoundError(
            f"unknown study {study_id!r}: experiments/{study_id}/manifest.yaml not found"
        )
    lock = build_enrollment_lock(study_id, pool_roots=pool_roots)
    output = _write_enrollment_lock(study_id, lock)
    logger.info(
        "wrote %s (%d enrolled runs)",
        to_repo_relative(output),
        len(lock["enrollment"]),
    )
    return output


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collect",
        description="Build experiments/<study>/reports/enrollment.lock.yaml.",
    )
    parser.add_argument("--study", required=True, help="Study id (folder name)")
    parser.add_argument(
        "--pool-root",
        action="append",
        type=Path,
        dest="pool_roots",
        help="Override pool roots (repeatable); defaults to runs/ + settings.output.directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    pool_roots = args.pool_roots if args.pool_roots else None
    try:
        # Default pool roots are computed lazily so test overrides land first.
        if pool_roots is None:
            pool_roots = default_pool_roots()
        collect_study(args.study, pool_roots=pool_roots)
    except (FileNotFoundError, DuplicateRunIdError, ValueError) as exc:
        print(f"collect: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
