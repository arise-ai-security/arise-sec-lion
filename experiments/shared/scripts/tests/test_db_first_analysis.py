"""Tests for DB-first experiment analysis helpers."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from experiments.shared.scripts import db_study_inputs
from experiments.shared.scripts.db_event_queries import DbEvent, TerminalStatusRow
from experiments.shared.scripts.db_event_queries import _event_from_record
from experiments.shared.scripts.db_family_metrics import AFamilyAggregator
from experiments.shared.scripts.db_first_analysis import (
    _build_claim_rows,
    _build_paired_rows,
    _classify_run,
    _csv_bytes,
)
from experiments.shared.scripts.db_study_inputs import CohortEntry, StudyDefinition


def test_load_study_definition_reads_only_design_and_lock_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Given: a minimal study with manifest, dataset, configs, and enrollment lock.
    repo_root = tmp_path
    monkeypatch.setattr(db_study_inputs, "get_repo_root", lambda: repo_root)
    study_dir = repo_root / "experiments" / "study-a"
    (study_dir / "configs").mkdir(parents=True)
    (study_dir / "reports").mkdir()
    (study_dir / "configs" / "A1.yaml").write_text("database: {}\n", encoding="utf-8")
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": "study-a",
                "dataset": "dataset.yaml",
                "cells": {
                    "A1": {
                        "group": "A",
                        "runner": "arise",
                        "config": "configs/A1.yaml",
                    }
                },
                "headline_cells": ["A1"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (study_dir / "dataset.yaml").write_text(
        yaml.safe_dump({"default_cves": ["cve-a"]}),
        encoding="utf-8",
    )
    (study_dir / "reports" / "enrollment.lock.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": "study-a",
                "enrollment": [
                    {
                        "run_id": "11111111-1111-1111-1111-111111111111",
                        "cell": "A1",
                        "task": "cve-a",
                        "replicate": 0,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # When
    study = db_study_inputs.load_study_definition("study-a")

    # Then
    assert study.study_id == "study-a"
    assert study.target_tasks == ("cve-a",)
    assert study.cohort[0].run_id == "11111111-1111-1111-1111-111111111111"
    assert study.input_paths[-1].name == "enrollment.lock.yaml"


def test_classify_run_marks_missing_cost_event_as_censored() -> None:
    # Given: a terminal successful run without WorkerCostRecorded.
    cohort = CohortEntry(
        run_id="11111111-1111-1111-1111-111111111111",
        cell="A1",
        task="cve-a",
        replicate=0,
    )
    terminal = TerminalStatusRow(
        run_id=cohort.run_id,
        has_run_started=True,
        has_run_completed=True,
        has_work_completed=True,
        has_work_failed=False,
        failure_reason="",
        run_status="completed",
        worker_cost_event_count=0,
        event_count=5,
        min_sequence=1,
        max_sequence=5,
    )

    # When
    validity = _classify_run(cohort, terminal)

    # Then
    assert validity.terminal_db_run
    assert validity.successful_db_run
    assert not validity.analysis_included
    assert validity.invalid_reasons == ("missing_cost_event",)


def test_a_family_aggregator_counts_task_tools_and_verdict_headers() -> None:
    # Given: A-family DB events with Claude Code style Input payloads.
    cohort = CohortEntry(
        run_id="11111111-1111-1111-1111-111111111111",
        cell="A1",
        task="cve-a",
        replicate=0,
    )
    write_payload = {
        "file_path": "/work/fixer-verdict.md",
        "content": "# Verdict: fixed\nPatch applies cleanly.",
    }
    events = [
        _thought("Tool: TaskCreate\nInput: " + json.dumps({"prompt": "build repro"}), 1),
        _thought("Writing: /work/fixer-verdict.md\nInput: " + json.dumps(write_payload), 2),
        _thought(
            "Running: compile\nInput: "
            + json.dumps({"command": "make test", "description": "compile fixer patch"}),
            3,
        ),
        DbEvent(
            aggregate_id=cohort.run_id,
            sequence_number=4,
            event_type="WorkerCostRecorded",
            payload={"tool_name": "claude_code", "cost_usd": 0.25, "prompt_tokens": 10},
        ),
        DbEvent(
            aggregate_id=cohort.run_id,
            sequence_number=5,
            event_type="RunCompleted",
            payload={
                "status": "completed",
                "duration_seconds": 12.0,
                "total_agents": 1,
                "completed_agents": 1,
                "failed_agents": 0,
            },
        ),
    ]

    # When
    row = AFamilyAggregator().aggregate(cohort, events)

    # Then
    assert row["source_authority"] == "db_events"
    assert row["task_create_count"] == 1
    assert row["write_tool_call_count"] == 1
    assert row["bash_tool_call_count"] == 1
    assert row["fixer_deliverable_evidence"] == 2
    assert row["verdict_header_count"] == 1
    assert row["worker_cost_usd"] == 0.25


def test_db_event_record_parser_preserves_decoded_jsonb_payload() -> None:
    # Given: an asyncpg-like record whose JSONB payload has already been decoded.
    record = {
        "aggregate_id": "11111111-1111-1111-1111-111111111111",
        "sequence_number": 7,
        "event_type": "ThoughtCaptured",
        "payload": {"output_type": "tool_use", "content": "Tool: Bash"},
        "occurred_at": None,
        "metadata": {},
    }

    # When
    event = _event_from_record(record)

    # Then: metric aggregators see the real payload instead of an empty dict.
    assert event.payload["output_type"] == "tool_use"


def test_claim_rows_accept_reported_values_only_on_exact_db_match() -> None:
    # Given: a small study whose DB-derived counts do not match the reported claim.
    study = _study(
        [
            CohortEntry("11111111-1111-1111-1111-111111111111", "A1", "cve-a", 0),
            CohortEntry("22222222-2222-2222-2222-222222222222", "A2", "cve-b", 0),
        ],
        target_tasks=("cve-a", "cve-b"),
    )
    validity = {
        study.cohort[0].run_id: _validity(study.cohort[0], successful=True),
        study.cohort[1].run_id: _validity(study.cohort[1], successful=False),
    }

    # When
    rows = _build_claim_rows(study, validity)

    # Then
    terminal = next(row for row in rows if row["claim"] == "terminal_db_runs")
    assert terminal["source_authority"] == "db_events"
    assert terminal["db_value"] == 2
    assert terminal["reported_value"] == 237
    assert terminal["accepted"] == 0


def test_paired_rows_include_pair_flags_and_metric_deltas() -> None:
    # Given: paired A1/A2 rows for the same task.
    study = _study(
        [
            CohortEntry("11111111-1111-1111-1111-111111111111", "A1", "cve-a", 0),
            CohortEntry("22222222-2222-2222-2222-222222222222", "A2", "cve-a", 0),
        ],
        target_tasks=("cve-a",),
    )
    metric_rows = [
        {
            "source_authority": "db_events",
            "run_id": study.cohort[0].run_id,
            "cell": "A1",
            "task": "cve-a",
            "replicate": 0,
            "total_cost_usd": 2.5,
            "tool_call_count": 10,
            "task_tool_call_count": 3,
            "verdict_header_count": 1,
            "run_duration_seconds": 20,
            "invalid_reasons": "",
        },
        {
            "source_authority": "db_events",
            "run_id": study.cohort[1].run_id,
            "cell": "A2",
            "task": "cve-a",
            "replicate": 0,
            "total_cost_usd": 1.0,
            "tool_call_count": 4,
            "task_tool_call_count": 0,
            "verdict_header_count": 0,
            "run_duration_seconds": 12,
            "invalid_reasons": "",
        },
    ]
    validity = {
        study.cohort[0].run_id: _validity(study.cohort[0], successful=True),
        study.cohort[1].run_id: _validity(study.cohort[1], successful=False),
    }

    # When
    rows = _build_paired_rows(study, metric_rows, validity)

    # Then
    assert rows[0]["source_authority"] == "db_events"
    assert rows[0]["pair_included"] == 1
    assert rows[0]["success_delta_a1_minus_a2"] == 1
    assert rows[0]["total_cost_delta_a1_minus_a2"] == 1.5
    assert rows[0]["tool_call_delta_a1_minus_a2"] == 6


def test_csv_writer_emits_source_authority_column_for_empty_tables() -> None:
    # Given/When
    payload = _csv_bytes([])

    # Then
    assert payload.decode("utf-8") == "source_authority\n"


def _thought(content: str, sequence_number: int) -> DbEvent:
    return DbEvent(
        aggregate_id="11111111-1111-1111-1111-111111111111",
        sequence_number=sequence_number,
        event_type="ThoughtCaptured",
        payload={"content": content, "output_type": "tool_use"},
    )


def _study(
    cohort: list[CohortEntry],
    *,
    target_tasks: tuple[str, ...],
) -> StudyDefinition:
    root = Path("/tmp/repo/experiments/study-a")
    return StudyDefinition(
        study_id="study-a",
        study_dir=root,
        manifest_path=root / "manifest.yaml",
        dataset_path=root / "dataset.yaml",
        enrollment_path=root / "reports" / "enrollment.lock.yaml",
        target_tasks=target_tasks,
        cells={
            "A1": db_study_inputs.StudyCell(
                name="A1",
                group="A",
                runner="arise",
                config_path=root / "configs" / "A1.yaml",
            ),
            "A2": db_study_inputs.StudyCell(
                name="A2",
                group="A",
                runner="arise",
                config_path=root / "configs" / "A2.yaml",
            ),
        },
        headline_cells=("A1", "A2"),
        cohort=tuple(cohort),
    )


def _validity(cohort: CohortEntry, *, successful: bool) -> object:
    terminal = TerminalStatusRow(
        run_id=cohort.run_id,
        has_run_started=True,
        has_run_completed=True,
        has_work_completed=successful,
        has_work_failed=not successful,
        failure_reason="" if successful else "assertion failed",
        run_status="completed" if successful else "failed",
        worker_cost_event_count=1,
        event_count=10,
        min_sequence=1,
        max_sequence=10,
    )
    return _classify_run(cohort, terminal)
