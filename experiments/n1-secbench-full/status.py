#!/usr/bin/env python3
"""Temporary N1 progress dump: done/pending CVE instances + key paths.

Run from the arise-sec-lion repo root::

    uv run python experiments/n1-secbench-full/status.py
    uv run python experiments/n1-secbench-full/status.py --all
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from _n1_experiment import (
    CELL_ID,
    EXPECTED_TASK_COUNT,
    REPO_ROOT,
    STUDY_ID,
    ExperimentError,
    load_definition,
    load_run_records,
    pending_tasks,
)


LOG_DIR = REPO_ROOT / "_run_logs" / STUDY_ID
RUNS_DIR = REPO_ROOT / "runs"
EXPORT_DEFAULT = Path.home() / f"{STUDY_ID}-export"
STUDY_DIR = REPO_ROOT / "experiments" / STUDY_ID
_PENDING_PREVIEW = 40


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="print every pending instance (default: first 40 only)",
    )
    return parser


def _latest_logs(limit: int = 5) -> list[Path]:
    if not LOG_DIR.is_dir():
        return []
    logs = sorted(LOG_DIR.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[:limit]


def _load_records():
    definition = load_definition()
    try:
        records = load_run_records(definition, event_backed_only=True)
        source = "event-backed manifests (Postgres + runs/)"
    except ExperimentError as exc:
        print(
            f"warning: event-store read failed ({exc}); using filesystem manifests only",
            file=sys.stderr,
        )
        records = load_run_records(definition, event_backed_only=False)
        source = "filesystem manifests only (not event-verified)"
    return definition, records, source


def _print_done(records) -> None:
    if not records:
        print("--- done instances ---")
        print("  (none)")
        print()
        return
    print(f"--- done instances (canonical first launch, {len(records)}) ---")
    width = max(len(r.task) for r in records)
    for record in records:
        print(
            f"  {record.exit_status:12}  {record.task:<{width}}  "
            f"run_id={record.run_id}"
        )
        print(f"  {'':12}  {record.run_dir / 'run_manifest.json'}")
    print()


def _print_pending(pending: list[str], *, show_all: bool) -> None:
    if not pending:
        print("--- pending instances ---")
        print("  (none — campaign complete for the full 300-task set)")
        print()
        return
    print(f"--- pending instances ({len(pending)}) ---")
    shown = pending if show_all else pending[:_PENDING_PREVIEW]
    for task in shown:
        print(f"  {task}")
    rest = len(pending) - len(shown)
    if rest > 0:
        print(f"  ... and {rest} more (pass --all to list every pending task)")
    print()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Multi-run "using earliest" notes are noise for a status dump.
    logging.getLogger("_n1_experiment").setLevel(logging.ERROR)

    definition, records, source = _load_records()
    selected = list(definition.tasks)
    pending = pending_tasks(selected, records)
    by_status = Counter(record.exit_status for record in records)

    print("=== N1 run status ===")
    print(f"study:      {STUDY_ID}  cell={CELL_ID}")
    print(f"dataset:    {len(selected)} tasks (expected {EXPECTED_TASK_COUNT})")
    print(f"authority:  {source}")
    print()
    print(f"done:       {len(records)}/{len(selected)}")
    print(f"pending:    {len(pending)}")
    if by_status:
        print("by status:")
        for status, count in sorted(by_status.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {status:16} {count}")
    else:
        print("by status:  (none)")
    print()

    _print_done(records)
    _print_pending(pending, show_all=args.all)

    latest = _latest_logs()
    print("--- important locations ---")
    print(f"  study dir:       {STUDY_DIR}")
    print(f"  config:          {STUDY_DIR / 'configs' / 'N1-openhands-linear.yaml'}")
    print(f"  dataset:         {STUDY_DIR / 'dataset.yaml'}")
    print(f"  env (local):     {STUDY_DIR / '.env'}")
    print(f"  run artifacts:   {RUNS_DIR}/<run_id>/")
    print(f"                   {RUNS_DIR}/<run_id>/run_manifest.json")
    print(f"  host run logs:   {LOG_DIR}/")
    if latest:
        print(f"  latest log:      {latest[0]}")
        for path in latest[1:]:
            print(f"  recent log:      {path}")
    else:
        print(f"  latest log:      (none yet under {LOG_DIR})")
    print(f"  enrollment lock: {STUDY_DIR / 'reports' / 'enrollment.lock.yaml'}")
    print(f"  export default:  {EXPORT_DEFAULT}/")
    print(f"                   {EXPORT_DEFAULT / 'export_manifest.json'}")
    print()

    print("--- finish the full experiment ---")
    print("  # from arise-sec-lion repo root (resumes pending tasks only)")
    print("  python3 experiments/n1-secbench-full/run.py --parallel 2 --batch-size 30")
    print()
    print("  # smoke")
    print("  python3 experiments/n1-secbench-full/run.py --smoke --parallel 2 --batch-size 2")
    print()
    print("  # force-rerun specific finished tasks (new attempts; canonical stays first)")
    print("  python3 experiments/n1-secbench-full/run.py --force --parallel 2 --batch-size 2 \\")
    print("    --instances gpac.cve-2023-5586")
    print()
    print("  # after full completion")
    print("  python3 experiments/n1-secbench-full/export.py")
    print()
    print("Notes:")
    print("  - Re-running run.py skips any task that already has a terminal exit_status")
    print("    (success or failed). Failed tasks need --force --instances to retry.")
    print("  - Kill mid-task: no terminal manifest → stays pending → rerun.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExperimentError, OSError, ValueError) as exc:
        print(f"status failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
