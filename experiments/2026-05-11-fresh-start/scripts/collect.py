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
    # BUG-METRIC4 split: ProbeStarted events (recon stage) used to share
    # tool_call_count with worker tool_use; they now have their own bucket
    # so manager-recon volume does not contaminate worker tool-call volume.
    "probe_count",
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
    # BUG-METRIC7: LLM-judge score per run (-1 sentinel when no judge stage
    # ran). At cell/study totals this is the sum of per-run scores with
    # sentinel propagation (any -1 contaminates the cohort total); consumers
    # divide by `runs` for mean.
    "judge_score",
    # BUG-METRIC1: count of Bash invocations matching the strict/narrow
    # cheating-attempt signatures (`git log`, `git show`, `git diff <ref>`,
    # `git reflog`). Per-run integer; cell/study totals are plain sums.
    "cheating_attempt_count",
    # Postcondition metrics (smoke-test-2026-05-12 follow-up).
    # `judge_ran` = real judge LLM call occurred (NOT the structural-check
    # sentinel). `judge_passed` = judge ran AND score >= 70.
    # `structural_check_passed` = the structural fallback fired (mutually
    # exclusive at run level with judge_ran).
    "judge_ran",
    "judge_passed",
    "structural_check_passed",
    # From RunCompleted payload; -1 sentinel when no RunCompleted observed.
    "total_agents",
    "completed_agents",
    "failed_agents",
    # Counts derived from RetryScheduled / RedecompositionTriggered events.
    "retry_count",
    "redecomposition_count",
]

# Filesystem-derived per-run booleans (0/1) checked against testcase/.
# Kept separate from METRIC_FIELDS because they cannot be computed from
# events.jsonl alone.
ARTIFACT_FIELDS = [
    "builder_artifacts_present",
    "exploiter_artifacts_present",
    "fixer_artifacts_present",
]

# JSON-encoded dict columns produced by metrics_from_events. Listed
# separately so the aggregation/finalization paths can merge them by
# summing same-keyed values rather than treating them as opaque strings.
DICT_FIELDS = [
    "cost_by_model",
    "cost_by_operation",
]

SUMMARY_FIELDS = [
    "cell",
    "runs",
    "deliverables_present",
    "event_files",
    "artifact_files_copied",
    "run_terminated_status",
    *ARTIFACT_FIELDS,
    *METRIC_FIELDS,
    "tool_calls_by_type",
    *DICT_FIELDS,
]

RUN_FIELDS = [
    "cell",
    "task",
    "replicate",
    "run_id",
    # Renamed from `exit_status` so the field name reflects that the value
    # came from run_manifest.json["exit_status"] but represents the run's
    # terminal status, not a CLI exit code. See findings doc for rationale.
    "run_terminated_status",
    "events_jsonl",
    "artifacts_path",
    "deliverables_present",
    *ARTIFACT_FIELDS,
    *METRIC_FIELDS,
    "tool_calls_by_type",
    *DICT_FIELDS,
]

TOTAL_FIELDS = [
    "scope",
    "runs",
    "deliverables_present",
    "event_files",
    "artifact_files_copied",
    *ARTIFACT_FIELDS,
    *METRIC_FIELDS,
    "tool_calls_by_type",
    *DICT_FIELDS,
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
    manifest = _load_manifest() or {}
    cells = (manifest.get("cells") or {})
    # BUG-CR1: respect the `headline_cells` allowlist so smoke configs
    # registered in `cells:` don't contaminate headline aggregates. Fall
    # back to all cells when the allowlist is absent (legacy manifests).
    headline = manifest.get("headline_cells")
    if isinstance(headline, list) and headline:
        return sorted(str(name) for name in headline if str(name) in cells)
    return sorted(cells.keys())


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
    replicate = int(run.get("replicate", 0) or 0)
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


def _file_nonempty(path: Path) -> bool:
    """Return True iff ``path`` exists, is a file, and has nonzero size.

    Several SEC-bench artifacts (notably ``repo_changes.diff``) are created
    even on partial failure but are 0 bytes — the size check is what makes
    "artifacts present" meaningful.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _check_artifacts(run_dir: Path | None) -> tuple[int, int, int]:
    """Inspect ``run_dir/testcase`` and return builder/exploiter/fixer flags.

    Builder: ``base_commit_hash`` AND ``packages.txt`` AND (``repo_changes.diff``
    nonempty OR ``src/build.sh`` exists). The parenthetical OR mirrors the
    smoke-test findings doc — SEC-bench-style runs sometimes ship build
    metadata as ``src/build.sh`` instead of a populated diff.

    Exploiter: ``repro.sh`` nonempty AND at least one ``poc.*`` file nonempty.

    Fixer: ``model_patch.diff`` nonempty.

    Returns three 0/1 ints (no booleans — CSV consumers treat them as counts).
    """
    if run_dir is None:
        return 0, 0, 0
    testcase = run_dir / "testcase"
    if not testcase.is_dir():
        return 0, 0, 0

    base_commit = _file_nonempty(testcase / "base_commit_hash")
    packages = _file_nonempty(testcase / "packages.txt")
    repo_diff = _file_nonempty(testcase / "repo_changes.diff")
    build_sh = (testcase / "src" / "build.sh").is_file()
    builder = int(base_commit and packages and (repo_diff or build_sh))

    repro = _file_nonempty(testcase / "repro.sh")
    poc_present = any(
        _file_nonempty(candidate) for candidate in testcase.glob("poc.*")
    )
    exploiter = int(repro and poc_present)

    fixer = int(_file_nonempty(testcase / "model_patch.diff"))

    return builder, exploiter, fixer


def _merge_cost_dict(target: dict[str, float], source: Any) -> None:
    """In-place merge: sum same-keyed floats from ``source`` into ``target``.

    Used for both `cost_by_model` and `cost_by_operation` so per-run dicts
    can roll up to cell totals without losing model/operation provenance.
    """
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        try:
            target[str(key)] = round(float(target.get(str(key), 0.0)) + float(value), 6)
        except (TypeError, ValueError):
            continue


def _repo_relative_existing(paths: set[Path]) -> list[str]:
    rels: list[str] = []
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        try:
            rels.append(path.resolve().relative_to(_REPO_ROOT).as_posix())
        except ValueError:
            logger.warning("omitting non-repo input from report provenance: %s", path)
    return list(dict.fromkeys(rels))


_SENTINEL_INT_FIELDS = frozenset({
    # Per-run -1 sentinels that must sticky-propagate at the cohort level so
    # an "any unknown ⇒ cohort total unknown" semantic is preserved.
    "judge_score",
    "total_agents",
    "completed_agents",
    "failed_agents",
})


def _add_metric_totals(target: dict[str, Any], metrics: dict[str, Any]) -> None:  # noqa: PLR0912
    for field in METRIC_FIELDS:
        current = target.get(field, 0)
        value = metrics.get(field, 0)
        # Audit N-7 completion: run_duration_seconds carries a -1.0 sentinel
        # at the per-run level when no RunCompleted was observed. Summing it
        # blindly poisons cell totals (e.g. 3 ok runs at 60s + 1 missing
        # produces 179.0 instead of "180.0 with one unknown"). Sticky-propagate
        # the sentinel: once any run is unmeasured, the cell total is unknown.
        if field == "run_duration_seconds":
            current_f = float(current)
            value_f = float(value)
            if current_f == -1.0 or value_f == -1.0:
                target[field] = -1.0
            else:
                target[field] = round(current_f + value_f, 6)
            continue
        if field in _SENTINEL_INT_FIELDS:
            # Mirror run_duration_seconds: a -1 sentinel in either operand
            # contaminates the cohort total so consumers cannot silently
            # under-attribute a missing RunCompleted payload as zero.
            current_i = int(current)
            value_i = int(value)
            if current_i == -1 or value_i == -1:
                target[field] = -1
            else:
                target[field] = current_i + value_i
            continue
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

    for dict_field in DICT_FIELDS:
        target_dict = target.setdefault(f"_{dict_field}", {})
        if not isinstance(target_dict, dict):
            target_dict = {}
            target[f"_{dict_field}"] = target_dict
        _merge_cost_dict(target_dict, metrics.get(dict_field))


def _finalize_row(row: dict[str, Any]) -> dict[str, Any]:
    if "_tool_calls_by_type" in row:
        breakdown = row.pop("_tool_calls_by_type", {})
        row["tool_calls_by_type"] = json.dumps(breakdown, sort_keys=True)
    else:
        row["tool_calls_by_type"] = str(row.get("tool_calls_by_type") or "{}")
    for dict_field in DICT_FIELDS:
        accum_key = f"_{dict_field}"
        if accum_key in row:
            payload = row.pop(accum_key, {})
            row[dict_field] = json.dumps(payload, sort_keys=True)
        else:
            value = row.get(dict_field)
            if isinstance(value, dict):
                row[dict_field] = json.dumps(value, sort_keys=True)
            else:
                row[dict_field] = str(value or "{}")
    for field in ("llm_cost_usd", "worker_cost_usd", "total_cost_usd"):
        row[field] = f"{float(row.get(field, 0.0)):.6f}"
    row["run_duration_seconds"] = f"{float(row.get('run_duration_seconds', 0.0)):.3f}"
    return row


def _filtered_runs(
    cell: str,
    task_scope: set[str],
) -> list[dict[str, Any]]:
    runs = load_runs(study_id=STUDY_ID, cells=[cell])
    # Audit N-2 completion: mirror collect.build_enrollment_lock's exclusion
    # of projection_status="failed" rows. Pre-fix the lockfile excluded the
    # failed row but summary.csv / run_metrics.csv still saw a clean zero
    # (events.jsonl absent → empty_metrics()), so the two artifacts disagreed
    # on cohort size. Silent zero is worse than dropped row.
    runs = [run for run in runs if run.get("projection_status") != "failed"]
    if not task_scope:
        return runs
    return [run for run in runs if run.get("task") in task_scope]


def _summarize(
    cells: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], set[Path]]:
    manifest = _load_manifest()
    dataset = _load_dataset(manifest)
    current_task_scope = _task_scope(dataset)
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
        **dict.fromkeys(ARTIFACT_FIELDS, 0),
        **dict.fromkeys(METRIC_FIELDS, 0),
        "_tool_calls_by_type": {},
        **{f"_{f}": {} for f in DICT_FIELDS},
    }

    for cell in cells:
        runs = _filtered_runs(cell, current_task_scope)
        row: dict[str, Any] = {
            "cell": cell,
            "runs": len(runs),
            "deliverables_present": 0,
            "event_files": 0,
            "artifact_files_copied": 0,
            # `run_terminated_status` aggregated as the count of runs in this
            # cell that reached the `success` terminal status (mirrors how
            # we treat other 0/1 per-run booleans). For per-run rows this
            # column carries the raw string instead.
            "run_terminated_status": 0,
            **dict.fromkeys(ARTIFACT_FIELDS, 0),
            **dict.fromkeys(METRIC_FIELDS, 0),
            "_tool_calls_by_type": {},
            **{f"_{f}": {} for f in DICT_FIELDS},
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
            builder_ok, exploiter_ok, fixer_ok = _check_artifacts(run_dir)
            terminated_status = str(run.get("exit_status") or "")

            row["deliverables_present"] = int(row["deliverables_present"]) + deliverables
            row["artifact_files_copied"] = int(row["artifact_files_copied"]) + artifact_count
            row["builder_artifacts_present"] = (
                int(row["builder_artifacts_present"]) + builder_ok
            )
            row["exploiter_artifacts_present"] = (
                int(row["exploiter_artifacts_present"]) + exploiter_ok
            )
            row["fixer_artifacts_present"] = (
                int(row["fixer_artifacts_present"]) + fixer_ok
            )
            if terminated_status == "success":
                row["run_terminated_status"] = int(row["run_terminated_status"]) + 1
            if events_jsonl:
                row["event_files"] = int(row["event_files"]) + 1
            _add_metric_totals(row, metrics)

            run_row: dict[str, Any] = {
                "cell": cell,
                "task": run.get("task") or "",
                "replicate": run.get("replicate", 0),
                "run_id": run_id,
                "run_terminated_status": terminated_status,
                "events_jsonl": events_jsonl,
                "artifacts_path": artifact_rel,
                "deliverables_present": deliverables,
                "builder_artifacts_present": builder_ok,
                "exploiter_artifacts_present": exploiter_ok,
                "fixer_artifacts_present": fixer_ok,
            }
            for field in METRIC_FIELDS:
                run_row[field] = metrics.get(field, 0)
            run_row["tool_calls_by_type"] = json.dumps(
                metrics.get("tool_calls_by_type") or {},
                sort_keys=True,
            )
            for dict_field in DICT_FIELDS:
                payload = metrics.get(dict_field) or {}
                run_row[dict_field] = json.dumps(
                    payload if isinstance(payload, dict) else {},
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
        for artifact_field in ARTIFACT_FIELDS:
            total_row[artifact_field] = (
                int(total_row[artifact_field]) + int(row[artifact_field])
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
