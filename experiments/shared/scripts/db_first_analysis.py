"""DB-first quantitative analysis for experiment studies.

The authoritative data contract is:

* study design and enrollment from YAML files under ``experiments/<study>``;
* run events from the Postgres ``events`` table.

This command never reads ``run_manifest.json`` or projected ``events.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import asyncpg
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from config import Settings
from experiments.shared.scripts._paths import get_repo_root, to_repo_relative
from experiments.shared.scripts.db_event_queries import (
    DbEvent,
    DbEventRepository,
    TerminalStatusRow,
)
from experiments.shared.scripts.db_family_metrics import (
    AggregationRegistry,
    is_provider_failure,
    is_timeout_or_no_output,
)
from experiments.shared.scripts.db_study_inputs import (
    CohortEntry,
    StudyDefinition,
    first_config_path,
    load_study_definition,
)
from experiments.shared.scripts.write_report import write_binary, write_md


logger = logging.getLogger(__name__)

SOURCE_AUTHORITY = "db_events"
REPORT_TEMPLATE = "experiments/shared/templates/db-claim-validation.md.j2"
REPORTED_TERMINAL_RUNS = 237
REPORTED_SUCCESSFUL_RUNS = 210
REPORTED_CVES_ATTEMPTED = 121


@dataclass(frozen=True)
class RunValidity:
    """Validity/censoring status for one enrolled run."""

    cohort: CohortEntry
    terminal: TerminalStatusRow
    invalid_reasons: tuple[str, ...]

    @property
    def attempted_db_run(self) -> bool:
        return self.terminal.event_count > 0

    @property
    def terminal_db_run(self) -> bool:
        return self.terminal.has_run_completed and (
            self.terminal.has_work_completed or self.terminal.has_work_failed
        )

    @property
    def successful_db_run(self) -> bool:
        return self.terminal_db_run and self.terminal.has_work_completed

    @property
    def analysis_included(self) -> bool:
        return self.terminal_db_run and not self.invalid_reasons

    @property
    def validity_status(self) -> str:
        return "valid" if self.analysis_included else "censored"


@dataclass(frozen=True)
class AnalysisTables:
    """All generated DB-first tables."""

    validity_rows: list[dict[str, Any]]
    inventory_rows: list[dict[str, Any]]
    per_run_metric_rows: list[dict[str, Any]]
    paired_rows: list[dict[str, Any]]
    orphan_rows: list[dict[str, Any]]
    claim_rows: list[dict[str, Any]]
    summary_rows: list[dict[str, Any]]


async def generate_db_first_analysis(
    *,
    study: StudyDefinition,
    connection_string: str,
) -> AnalysisTables:
    """Query Postgres and build all DB-first analysis tables."""
    run_ids = [entry.run_id for entry in study.cohort]
    repository = DbEventRepository(connection_string)
    await repository.connect()
    try:
        terminal_by_run = await repository.fetch_terminal_status(run_ids)
        inventory_raw = await repository.fetch_event_inventory(run_ids)
        events_by_run = await repository.fetch_events_for_aggregates(run_ids)
        orphan_rows = await repository.fetch_orphan_aggregate_summaries(run_ids)
    finally:
        await repository.disconnect()

    cohort_by_run = {entry.run_id: entry for entry in study.cohort}
    validity_by_run = {
        entry.run_id: _classify_run(entry, terminal_by_run[entry.run_id])
        for entry in study.cohort
    }

    inventory_rows = _attach_inventory_metadata(inventory_raw, cohort_by_run)
    per_run_metric_rows = _build_per_run_metrics(study, events_by_run, validity_by_run)
    validity_rows = [_validity_row(item) for item in validity_by_run.values()]
    paired_rows = _build_paired_rows(study, per_run_metric_rows, validity_by_run)
    claim_rows = _build_claim_rows(study, validity_by_run)
    summary_rows = _build_summary_rows(study, validity_by_run)

    return AnalysisTables(
        validity_rows=validity_rows,
        inventory_rows=inventory_rows,
        per_run_metric_rows=per_run_metric_rows,
        paired_rows=paired_rows,
        orphan_rows=orphan_rows,
        claim_rows=claim_rows,
        summary_rows=summary_rows,
    )


def write_analysis_outputs(
    *,
    study: StudyDefinition,
    tables: AnalysisTables,
    output_dir: Path | None = None,
) -> list[Path]:
    """Write requested CSV/MD artifacts with report provenance."""
    reports_dir = output_dir or (study.study_dir / "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    inputs = [to_repo_relative(path) for path in study.input_paths]
    script = "experiments/shared/scripts/db_first_analysis.py"

    written: list[Path] = []
    csv_specs = [
        ("db_run_validity.csv", tables.validity_rows),
        ("db_event_inventory.csv", tables.inventory_rows),
        ("db_per_run_metrics.csv", tables.per_run_metric_rows),
        ("db_paired_a1_a2_metrics.csv", tables.paired_rows),
        ("db_orphan_runs.csv", tables.orphan_rows),
    ]
    for filename, rows in csv_specs:
        path = reports_dir / filename
        written.append(
            write_binary(
                path=path,
                content=_csv_bytes(rows),
                script=script,
                inputs=inputs,
            )
        )

    markdown = _render_claim_validation(
        study=study,
        tables=tables,
        generated_files=[to_repo_relative(path) for path in written],
    )
    written.append(
        write_md(
            path=reports_dir / "db_claim_validation.md",
            content=markdown,
            script=script,
            template=REPORT_TEMPLATE,
            inputs=[*inputs, *(to_repo_relative(path) for path in written)],
        )
    )
    return written


def _classify_run(cohort: CohortEntry, terminal: TerminalStatusRow) -> RunValidity:
    reasons: list[str] = []
    if terminal.event_count == 0:
        reasons.append("no_db_events")
    if not terminal.has_run_started:
        reasons.append("no_run_started")
    if not terminal.has_run_completed:
        reasons.append("no_run_completed")
    if not terminal.has_work_completed and not terminal.has_work_failed:
        reasons.append("no_work_terminal_event")
    if terminal.has_work_failed and is_provider_failure(terminal.failure_reason):
        reasons.append("work_failed_api_provider_failure")
    if terminal.has_work_failed and is_timeout_or_no_output(terminal.failure_reason):
        reasons.append("timeout_no_output_termination")
    if (
        terminal.has_run_completed
        and (terminal.has_work_completed or terminal.has_work_failed)
        and terminal.worker_cost_event_count == 0
    ):
        reasons.append("missing_cost_event")
    return RunValidity(cohort=cohort, terminal=terminal, invalid_reasons=tuple(reasons))


def _validity_row(validity: RunValidity) -> dict[str, Any]:
    cohort = validity.cohort
    terminal = validity.terminal
    return {
        "source_authority": SOURCE_AUTHORITY,
        "run_id": cohort.run_id,
        "cell": cohort.cell,
        "task": cohort.task,
        "replicate": cohort.replicate,
        "attempted_db_run": int(validity.attempted_db_run),
        "terminal_db_run": int(validity.terminal_db_run),
        "successful_db_run": int(validity.successful_db_run),
        "analysis_included": int(validity.analysis_included),
        "validity_status": validity.validity_status,
        "invalid_reasons": ";".join(validity.invalid_reasons),
        "has_run_started": int(terminal.has_run_started),
        "has_run_completed": int(terminal.has_run_completed),
        "has_work_completed": int(terminal.has_work_completed),
        "has_work_failed": int(terminal.has_work_failed),
        "failure_reason": terminal.failure_reason,
        "run_status": terminal.run_status,
        "worker_cost_event_count": terminal.worker_cost_event_count,
        "event_count": terminal.event_count,
        "min_sequence": terminal.min_sequence,
        "max_sequence": terminal.max_sequence,
    }


def _attach_inventory_metadata(
    inventory_raw: list[dict[str, Any]],
    cohort_by_run: dict[str, CohortEntry],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in inventory_raw:
        cohort = cohort_by_run.get(str(item["run_id"]))
        if cohort is None:
            continue
        rows.append(
            {
                "source_authority": SOURCE_AUTHORITY,
                "cell": cohort.cell,
                "task": cohort.task,
                "replicate": cohort.replicate,
                "run_id": cohort.run_id,
                "event_type": item["event_type"],
                "n": item["n"],
            }
        )
    return rows


def _build_per_run_metrics(
    study: StudyDefinition,
    events_by_run: dict[str, list[DbEvent]],
    validity_by_run: dict[str, RunValidity],
) -> list[dict[str, Any]]:
    registry = AggregationRegistry.default()
    rows: list[dict[str, Any]] = []
    for cohort in study.cohort:
        cell = study.cells.get(cohort.cell)
        if cell is None:
            raise ValueError(f"enrollment references undeclared cell {cohort.cell!r}")
        events = events_by_run.get(cohort.run_id, [])
        row = registry.for_family(cell.group).aggregate(cohort, events)
        validity = validity_by_run[cohort.run_id]
        row.update(
            {
                "attempted_db_run": int(validity.attempted_db_run),
                "terminal_db_run": int(validity.terminal_db_run),
                "successful_db_run": int(validity.successful_db_run),
                "analysis_included": int(validity.analysis_included),
                "validity_status": validity.validity_status,
                "invalid_reasons": ";".join(validity.invalid_reasons),
            }
        )
        rows.append(row)
    return rows


def _build_paired_rows(
    study: StudyDefinition,
    metric_rows: list[dict[str, Any]],
    validity_by_run: dict[str, RunValidity],
) -> list[dict[str, Any]]:
    headline = [cell for cell in study.headline_cells if cell in {"A1", "A2"}]
    if set(headline) != {"A1", "A2"}:
        return []

    by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in metric_rows:
        if row.get("cell") in {"A1", "A2"}:
            by_key[(str(row["task"]), int(row["replicate"]), str(row["cell"]))] = row

    keys = sorted({(task, replicate) for task, replicate, _cell in by_key})
    rows: list[dict[str, Any]] = []
    for task, replicate in keys:
        a1 = by_key.get((task, replicate, "A1"))
        a2 = by_key.get((task, replicate, "A2"))
        a1_validity = validity_by_run.get(str(a1.get("run_id"))) if a1 else None
        a2_validity = validity_by_run.get(str(a2.get("run_id"))) if a2 else None
        pair_observed = bool(
            a1_validity
            and a2_validity
            and a1_validity.attempted_db_run
            and a2_validity.attempted_db_run
        )
        pair_terminal = bool(
            a1_validity
            and a2_validity
            and a1_validity.terminal_db_run
            and a2_validity.terminal_db_run
        )
        pair_included = bool(
            a1_validity
            and a2_validity
            and a1_validity.analysis_included
            and a2_validity.analysis_included
        )
        rows.append(
            {
                "source_authority": SOURCE_AUTHORITY,
                "task": task,
                "replicate": replicate,
                "a1_run_id": _cell_value(a1, "run_id"),
                "a2_run_id": _cell_value(a2, "run_id"),
                "pair_observed": int(pair_observed),
                "pair_terminal": int(pair_terminal),
                "pair_included": int(pair_included),
                "a1_successful_db_run": _validity_bool(a1_validity, "successful_db_run"),
                "a2_successful_db_run": _validity_bool(a2_validity, "successful_db_run"),
                "success_delta_a1_minus_a2": _delta_bool(
                    a1_validity,
                    a2_validity,
                    "successful_db_run",
                ),
                "total_cost_delta_a1_minus_a2": _metric_delta(a1, a2, "total_cost_usd"),
                "worker_cost_delta_a1_minus_a2": _metric_delta(a1, a2, "worker_cost_usd"),
                "duration_delta_a1_minus_a2": _metric_delta(
                    a1,
                    a2,
                    "run_duration_seconds",
                ),
                "tool_call_delta_a1_minus_a2": _metric_delta(a1, a2, "tool_call_count"),
                "task_tool_delta_a1_minus_a2": _metric_delta(a1, a2, "task_tool_call_count"),
                "verdict_header_delta_a1_minus_a2": _metric_delta(
                    a1,
                    a2,
                    "verdict_header_count",
                ),
                "a1_invalid_reasons": _cell_value(a1, "invalid_reasons"),
                "a2_invalid_reasons": _cell_value(a2, "invalid_reasons"),
            }
        )
    return rows


def _build_claim_rows(
    study: StudyDefinition,
    validity_by_run: dict[str, RunValidity],
) -> list[dict[str, Any]]:
    attempted = [row for row in validity_by_run.values() if row.attempted_db_run]
    terminal = [row for row in validity_by_run.values() if row.terminal_db_run]
    successful = [row for row in validity_by_run.values() if row.successful_db_run]
    attempted_tasks = {row.cohort.task for row in attempted}
    claim_specs = [
        ("target_cves_from_dataset", "", len(study.target_tasks)),
        ("enrolled_runs_from_lockfile", "", len(study.cohort)),
        ("attempted_db_runs", "", len(attempted)),
        ("terminal_db_runs", REPORTED_TERMINAL_RUNS, len(terminal)),
        ("successful_db_runs", REPORTED_SUCCESSFUL_RUNS, len(successful)),
        ("distinct_cves_attempted", REPORTED_CVES_ATTEMPTED, len(attempted_tasks)),
    ]
    rows: list[dict[str, Any]] = []
    for claim, reported, db_value in claim_specs:
        accepted = "" if reported == "" else int(int(reported) == db_value)
        rows.append(
            {
                "source_authority": SOURCE_AUTHORITY,
                "claim": claim,
                "reported_value": reported,
                "db_value": db_value,
                "accepted": accepted,
            }
        )
    return rows


def _build_summary_rows(
    study: StudyDefinition,
    validity_by_run: dict[str, RunValidity],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cell in sorted({entry.cell for entry in study.cohort}):
        cell_rows = [row for row in validity_by_run.values() if row.cohort.cell == cell]
        rows.append(
            {
                "source_authority": SOURCE_AUTHORITY,
                "cell": cell,
                "enrolled_runs": len(cell_rows),
                "attempted_db_runs": sum(1 for row in cell_rows if row.attempted_db_run),
                "terminal_db_runs": sum(1 for row in cell_rows if row.terminal_db_run),
                "successful_db_runs": sum(1 for row in cell_rows if row.successful_db_run),
                "analysis_included_runs": sum(1 for row in cell_rows if row.analysis_included),
            }
        )
    return rows


def _render_claim_validation(
    *,
    study: StudyDefinition,
    tables: AnalysisTables,
    generated_files: list[str],
) -> str:
    env = Environment(
        loader=FileSystemLoader(get_repo_root()),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(REPORT_TEMPLATE)
    return template.render(
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        study_id=study.study_id,
        target_cve_count=len(study.target_tasks),
        enrolled_run_count=len(study.cohort),
        generated_files=generated_files,
        summary_table=_markdown_table(tables.summary_rows),
        claim_table=_markdown_table(tables.claim_rows),
        invalid_reason_table=_markdown_table(_invalid_reason_rows(tables.validity_rows)),
        orphan_count=len(tables.orphan_rows),
    )


def _invalid_reason_rows(validity_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in validity_rows:
        reasons = str(row.get("invalid_reasons") or "")
        if not reasons:
            continue
        for reason in reasons.split(";"):
            counts[reason] = counts.get(reason, 0) + 1
    return [
        {
            "source_authority": SOURCE_AUTHORITY,
            "invalid_reason": reason,
            "run_count": count,
        }
        for reason, count in sorted(counts.items())
    ]


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "| source_authority |\n|---|\n| db_events |"
    columns = _ordered_columns(rows)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_markdown_value(row.get(col)) for col in columns) + " |")
    return "\n".join(lines)


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    columns = _ordered_columns(rows)
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row.get(column)) for column in columns})
    return buffer.getvalue().encode("utf-8")


def _ordered_columns(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "source_authority",
        "run_id",
        "cell",
        "task",
        "replicate",
        "family",
        "event_type",
        "n",
    ]
    seen: set[str] = set()
    columns: list[str] = []
    for key in preferred:
        if any(key in row for row in rows):
            columns.append(key)
            seen.add(key)
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    if not columns:
        return ["source_authority"]
    return columns


def _csv_value(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str | int | float):
        return value
    return json.dumps(value, sort_keys=True)


def _markdown_value(value: Any) -> str:
    text = str(_csv_value(value))
    return text.replace("|", "\\|").replace("\n", "<br>")


def _cell_value(row: dict[str, Any] | None, key: str) -> Any:
    if row is None:
        return ""
    return row.get(key, "")


def _metric_delta(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    key: str,
) -> float | str:
    if left is None or right is None:
        return ""
    return round(_float(left.get(key)) - _float(right.get(key)), 6)


def _validity_bool(validity: RunValidity | None, attr: str) -> int | str:
    if validity is None:
        return ""
    return int(bool(getattr(validity, attr)))


def _delta_bool(
    left: RunValidity | None,
    right: RunValidity | None,
    attr: str,
) -> int | str:
    if left is None or right is None:
        return ""
    return int(bool(getattr(left, attr))) - int(bool(getattr(right, attr)))


def _float(value: Any) -> float:
    if isinstance(value, bool) or value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _connection_string(args: argparse.Namespace, study: StudyDefinition) -> str:
    if args.database_url:
        return str(args.database_url)
    config_path = args.config or first_config_path(study)
    return Settings.from_yaml(config_path).database.connection_string


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE env files without echoing secrets."""
    if not path.is_file():
        raise FileNotFoundError(path)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db_first_analysis",
        description="Generate DB-authoritative experiment analysis tables.",
    )
    parser.add_argument("--study", required=True, help="Study id or study directory")
    parser.add_argument("--config", type=Path, help="Config path used to resolve Postgres")
    parser.add_argument("--database-url", help="Explicit Postgres connection string")
    parser.add_argument("--env-file", type=Path, help="Optional KEY=VALUE env file")
    parser.add_argument("--output-dir", type=Path, help="Override reports output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    if args.env_file is not None:
        _load_env_file(args.env_file)
    try:
        study = load_study_definition(args.study)
        tables = asyncio.run(
            generate_db_first_analysis(
                study=study,
                connection_string=_connection_string(args, study),
            )
        )
        written = write_analysis_outputs(
            study=study,
            tables=tables,
            output_dir=args.output_dir,
        )
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        OSError,
        asyncpg.PostgresError,
    ) as exc:
        print(f"db_first_analysis: {exc}", file=sys.stderr)
        return 1
    for path in written:
        logger.info("wrote %s", to_repo_relative(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
