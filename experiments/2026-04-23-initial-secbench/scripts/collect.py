"""Collect run manifests, metrics, and artifacts for this study.

The report is scripts-first: numeric values in report.md come from the CSVs
written here, not from manual prose. This script also copies run-level traces
and testcase artifacts into ``experiments/<study>/artifacts/`` so the study
folder is self-contained enough for later inspection.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import sys
from csv import DictWriter
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from experiments.shared.scripts import collect as shared_collect  # noqa: E402
from experiments.shared.scripts.load_runs import iter_run_manifests, load_runs  # noqa: E402
from experiments.shared.scripts.run_metrics import (  # noqa: E402
    empty_metrics,
    metrics_from_events_jsonl,
)
from experiments.shared.scripts.write_report import write_binary  # noqa: E402


STUDY_DIR = Path(__file__).resolve().parent.parent
STUDY_ID = STUDY_DIR.name


logger = logging.getLogger(__name__)

METRIC_FIELDS = [
    "event_count",
    "prompt_sent_count",
    "tool_call_count",
    "tool_result_count",
    "thinking_event_count",
    "thinking_chars",
    "worker_output_event_count",
    "worker_cost_event_count",
    "tokens_prompt",
    "tokens_completion",
    "tokens_total",
    "tokens_reasoning",
    # Audit N-3: cache token buckets surface as their own columns so
    # cross-adapter (Claude-SDK vs OpenHands) token comparisons stay
    # symmetric and auditable.
    "tokens_cache_read",
    "tokens_cache_write",
    "llm_cost_usd",
    "worker_cost_usd",
    "total_cost_usd",
    "run_duration_seconds",
    # Audit N-7: number of RunCompleted events observed. 0 means we used
    # the -1.0 sentinel for run_duration_seconds; >1 signals an upstream
    # bug emitting duplicate completion events.
    "run_completed_count",
]

SUMMARY_FIELDS = [
    "cell",
    "runs",
    "deliverables_present",
    "event_files",
    "artifact_files_copied",
    *METRIC_FIELDS,
    "tool_calls_by_type",
]

RUN_FIELDS = [
    "cell",
    "task",
    "replicate",
    "run_id",
    "exit_status",
    "events_jsonl",
    "artifacts_path",
    "deliverables_present",
    *METRIC_FIELDS,
    "tool_calls_by_type",
]

TOTAL_FIELDS = [
    "scope",
    "runs",
    "deliverables_present",
    "event_files",
    "artifact_files_copied",
    *METRIC_FIELDS,
    "tool_calls_by_type",
]

_TRACE_FILENAMES = (
    "run_manifest.json",
    "events.jsonl",
    "effective_config.yaml",
    "stdout_stderr.log",
)


def _load_manifest() -> dict[str, Any]:
    return yaml.safe_load((STUDY_DIR / "manifest.yaml").read_text(encoding="utf-8")) or {}


def _load_dataset(manifest: dict[str, Any]) -> dict[str, Any]:
    dataset_rel = str(manifest.get("dataset") or "dataset.yaml")
    return yaml.safe_load((STUDY_DIR / dataset_rel).read_text(encoding="utf-8")) or {}


def _load_cells() -> list[str]:
    manifest = _load_manifest()
    return sorted((manifest or {}).get("cells", {}).keys())


def _task_scope(dataset: dict[str, Any]) -> set[str]:
    tasks: set[str] = set(dataset.get("default_cves") or [])
    for override in (dataset.get("per_cell_overrides") or {}).values():
        if isinstance(override, dict) and override.get("subset") is not None:
            tasks.update(str(task) for task in override.get("subset") or [])
    return tasks


def _run_manifest_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for manifest_path in iter_run_manifests():
        try:
            record = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        run_id = record.get("run_id")
        if isinstance(run_id, str):
            index.setdefault(run_id, manifest_path)
    return index


def _deliverable_count(run: dict[str, Any]) -> int:
    deliverables = run.get("deliverables") or {}
    if not isinstance(deliverables, dict):
        return 0
    return sum(1 for value in deliverables.values() if value)


def _copy_artifacts(run: dict[str, Any], run_dir: Path | None) -> tuple[str, int]:
    run_id = str(run.get("run_id") or "")
    if not run_id or run_dir is None or not run_dir.is_dir():
        return "", 0

    cell = str(run.get("cell") or "unknown-cell")
    task = str(run.get("task") or "unknown-task")
    replicate = int(run.get("replicate", run.get("attempt", 0)) or 0)
    destination = STUDY_DIR / "artifacts" / cell / task / f"replicate-{replicate}" / run_id

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    copied = 0
    for filename in _TRACE_FILENAMES:
        source = run_dir / filename
        if source.is_file():
            shutil.copy2(source, destination / filename)
            copied += 1

    testcase_source = run_dir / "testcase"
    if testcase_source.is_dir():
        testcase_dest = destination / "testcase"
        shutil.copytree(testcase_source, testcase_dest, dirs_exist_ok=True)
        copied += sum(1 for item in testcase_dest.rglob("*") if item.is_file())

    return destination.relative_to(STUDY_DIR.parent.parent).as_posix(), copied


def _metrics_for_run(run_dir: Path | None) -> tuple[dict[str, Any], str]:
    if run_dir is None:
        return empty_metrics(), ""
    events_jsonl = run_dir / "events.jsonl"
    if not events_jsonl.is_file():
        return empty_metrics(), ""
    return metrics_from_events_jsonl(events_jsonl), events_jsonl.as_posix()


def _repo_relative_existing(paths: set[Path]) -> list[str]:
    """Return existing repo-relative file paths suitable for provenance metadata."""
    rels: list[str] = []
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        try:
            rels.append(path.resolve().relative_to(_REPO_ROOT).as_posix())
        except ValueError:
            logger.warning("omitting non-repo input from report provenance: %s", path)
    return list(dict.fromkeys(rels))


def _add_metric_totals(target: dict[str, Any], metrics: dict[str, Any]) -> None:
    for field in METRIC_FIELDS:
        current = target.get(field, 0)
        value = metrics.get(field, 0)
        if isinstance(current, float) or isinstance(value, float):
            target[field] = round(float(current) + float(value), 6)
        else:
            target[field] = int(current) + int(value)

    breakdown = target.setdefault("_tool_calls_by_type", {})
    if not isinstance(breakdown, dict):
        breakdown = {}
        target["_tool_calls_by_type"] = breakdown
    metric_breakdown = (
        metrics.get("tool_calls_by_type") or metrics.get("_tool_calls_by_type") or {}
    )
    if isinstance(metric_breakdown, dict):
        for tool, count in metric_breakdown.items():
            breakdown[str(tool)] = int(breakdown.get(str(tool), 0)) + int(count)


def _finalize_row(row: dict[str, Any]) -> dict[str, Any]:
    if "_tool_calls_by_type" in row:
        breakdown = row.pop("_tool_calls_by_type", {})
        row["tool_calls_by_type"] = json.dumps(breakdown, sort_keys=True)
    else:
        row["tool_calls_by_type"] = str(row.get("tool_calls_by_type") or "{}")
    for field in ("llm_cost_usd", "worker_cost_usd", "total_cost_usd"):
        row[field] = f"{float(row.get(field, 0.0)):.6f}"
    row["run_duration_seconds"] = f"{float(row.get('run_duration_seconds', 0.0)):.3f}"
    return row


def _filtered_runs(
    cell: str,
    task_scope: set[str],
    *,
    exclude_legacy_migration: bool,
) -> list[dict[str, Any]]:
    runs = load_runs(study_id=STUDY_ID, cells=[cell])
    if exclude_legacy_migration:
        runs = [run for run in runs if run.get("legacy_migration") is not True]
    if not task_scope:
        return runs
    return [run for run in runs if run.get("task") in task_scope]


def _summarize(
    cells: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], set[Path]]:
    manifest = _load_manifest()
    dataset = _load_dataset(manifest)
    current_task_scope = _task_scope(dataset)
    exclude_legacy_migration = bool(manifest.get("exclude_legacy_migration", False))
    manifests_by_run_id = _run_manifest_index()
    runtime_inputs: set[Path] = set()

    rows: list[dict[str, object]] = []
    run_rows: list[dict[str, object]] = []
    total_row: dict[str, Any] = {
        "scope": "all_cells",
        "runs": 0,
        "deliverables_present": 0,
        "event_files": 0,
        "artifact_files_copied": 0,
        **dict.fromkeys(METRIC_FIELDS, 0),
        "_tool_calls_by_type": {},
    }

    for cell in cells:
        runs = _filtered_runs(
            cell,
            current_task_scope,
            exclude_legacy_migration=exclude_legacy_migration,
        )
        row: dict[str, Any] = {
            "cell": cell,
            "runs": len(runs),
            "deliverables_present": 0,
            "event_files": 0,
            "artifact_files_copied": 0,
            **dict.fromkeys(METRIC_FIELDS, 0),
            "_tool_calls_by_type": {},
        }
        for run in runs:
            run_id = str(run.get("run_id") or "")
            run_manifest = manifests_by_run_id.get(run_id)
            if run_manifest is not None:
                runtime_inputs.add(run_manifest)
            run_dir = run_manifest.parent if run_manifest is not None else None
            metrics, events_jsonl = _metrics_for_run(run_dir)
            if events_jsonl:
                runtime_inputs.add(Path(events_jsonl))
            artifact_rel, artifact_count = _copy_artifacts(run, run_dir)
            deliverables = _deliverable_count(run)

            row["deliverables_present"] = int(row["deliverables_present"]) + deliverables
            row["artifact_files_copied"] = int(row["artifact_files_copied"]) + artifact_count
            if events_jsonl:
                row["event_files"] = int(row["event_files"]) + 1
            _add_metric_totals(row, metrics)

            run_row: dict[str, Any] = {
                "cell": cell,
                "task": run.get("task") or "",
                "replicate": run.get("replicate", run.get("attempt", 0)),
                "run_id": run_id,
                "exit_status": run.get("exit_status") or "",
                "events_jsonl": events_jsonl,
                "artifacts_path": artifact_rel,
                "deliverables_present": deliverables,
            }
            for field in METRIC_FIELDS:
                run_row[field] = metrics.get(field, 0)
            run_row["tool_calls_by_type"] = json.dumps(
                metrics.get("tool_calls_by_type") or {},
                sort_keys=True,
            )
            run_rows.append(_finalize_row(run_row))

        _add_metric_totals(total_row, row)
        total_row["runs"] = int(total_row["runs"]) + int(row["runs"])
        total_row["deliverables_present"] = (
            int(total_row["deliverables_present"]) + int(row["deliverables_present"])
        )
        total_row["event_files"] = int(total_row["event_files"]) + int(row["event_files"])
        total_row["artifact_files_copied"] = (
            int(total_row["artifact_files_copied"]) + int(row["artifact_files_copied"])
        )
        rows.append(_finalize_row(row))

    return rows, run_rows, _finalize_row(total_row), runtime_inputs


def _csv_bytes(rows: list[dict[str, object]], fieldnames: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = DictWriter(
        buffer,
        fieldnames=fieldnames,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    shared_collect.collect_study(STUDY_ID)
    artifacts_root = STUDY_DIR / "artifacts"
    if artifacts_root.is_dir():
        shutil.rmtree(artifacts_root)
    cells = _load_cells()
    rows, run_rows, total_row, runtime_inputs = _summarize(cells)

    rel_study = STUDY_DIR.relative_to(STUDY_DIR.parent.parent).as_posix()
    script_rel = f"{rel_study}/scripts/collect.py"
    manifest_rel = f"{rel_study}/manifest.yaml"
    dataset_rel = f"{rel_study}/dataset.yaml"
    enrollment_rel = f"{rel_study}/reports/{shared_collect.ENROLLMENT_LOCK_FILENAME}"
    provenance_inputs = [
        manifest_rel,
        dataset_rel,
        enrollment_rel,
        *_repo_relative_existing(runtime_inputs),
    ]

    write_binary(
        path=f"{rel_study}/reports/tables/summary.csv",
        content=_csv_bytes(rows, SUMMARY_FIELDS),
        script=script_rel,
        inputs=provenance_inputs,
    )
    write_binary(
        path=f"{rel_study}/reports/tables/run_metrics.csv",
        content=_csv_bytes(run_rows, RUN_FIELDS),
        script=script_rel,
        inputs=provenance_inputs,
    )
    write_binary(
        path=f"{rel_study}/reports/tables/totals.csv",
        content=_csv_bytes([total_row], TOTAL_FIELDS),
        script=script_rel,
        inputs=provenance_inputs,
    )
    logger.info("wrote summary/run metrics/totals for %d cells", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
