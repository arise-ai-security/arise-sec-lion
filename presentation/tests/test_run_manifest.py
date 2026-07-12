"""Tests for RunResult and RunPersistence.write_run_manifest."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from core.query.projections.models import CostSummary, ProjectionSummary
from presentation.cli import RunResult
from presentation.persistence.run_persistence import RunPersistence
from presentation.tests.test_invocation_hash import _make_settings


if TYPE_CHECKING:
    from pathlib import Path


# -----------------------------
# RunResult (presentation DTO)
# -----------------------------


def test_run_result_is_frozen() -> None:
    # Given: a constructed RunResult.
    result = RunResult(root_id=uuid4(), status="success")

    # When: attempting to mutate it.
    # Then: it raises FrozenInstanceError (frozen dataclass guarantee).
    with pytest.raises(FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]


@pytest.mark.parametrize("status", ["success", "timeout", "failed"])
def test_run_result_accepts_documented_statuses(status: str) -> None:
    # Given/When
    result = RunResult(root_id=uuid4(), status=status)  # type: ignore[arg-type]

    # Then
    assert result.status == status


# ------------------------------
# RunPersistence.write_run_manifest
# ------------------------------


def _summary_with_cost(prompt: int, completion: int, **extra: float) -> ProjectionSummary:
    cost = CostSummary(
        total_cost_usd=sum(extra.values()),
        llm_cost_usd=sum(extra.values()),
        worker_cost_usd=0.0,
        total_tokens=prompt + completion,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_by_model=dict(extra),
    )
    empty = ProjectionSummary.empty()
    return ProjectionSummary(
        total_events=empty.total_events,
        events_by_type=empty.events_by_type,
        agents_involved=empty.agents_involved,
        first_event=empty.first_event,
        last_event=empty.last_event,
        error_count=empty.error_count,
        errors=empty.errors,
        node_counts=empty.node_counts,
        cost=cost,
        execution_time=empty.execution_time,
    )


@pytest.mark.asyncio
async def test_write_run_manifest_emits_full_payload(tmp_path: Path) -> None:
    # Given: persistence pointed at a tmp runs dir, and a run_id whose
    # testcase/ directory carries two files.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()
    testcase = tmp_path / str(run_id) / "testcase"
    testcase.mkdir(parents=True)
    (testcase / "model_patch.diff").write_text("diff --git\n")
    (testcase / "repro.sh").write_text("#!/bin/sh\n")

    settings = _make_settings()
    summary = _summary_with_cost(prompt=12345, completion=678, **{"o3": 0.41, "openai/o3": 0.29})

    started = datetime(2026, 4, 22, 14, 10, 0, tzinfo=UTC)
    ended = datetime(2026, 4, 22, 14, 45, 12, tzinfo=UTC)

    # When: writing the manifest.
    path = await persistence.write_run_manifest(
        run_id,
        settings=settings,
        task="gpac.cve-2021-40575",
        domain_context_path=None,
        exit_status="success",
        wall_started_at=started,
        wall_ended_at=ended,
        summary=summary,
    )

    # Then: the file exists at the canonical location.
    assert path == tmp_path / str(run_id) / "run_manifest.json"
    assert path.exists()

    payload = json.loads(path.read_text())

    # And: the top-level shape matches the spec.
    assert payload["run_id"] == str(run_id)
    assert payload["kind"] == "ours"
    assert payload["started_at"].endswith("Z")
    assert payload["ended_at"].endswith("Z")
    assert payload["exit_status"] == "success"
    assert payload["models"] == {
        "boss": settings.boss.model,
        "manager": settings.manager.model,
        "worker": settings.worker.model,
    }
    assert payload["summary_available"] is True
    assert payload["tokens"] == {"prompt": 12345, "completion": 678}
    assert payload["costs_by_model"] == {"o3": 0.41, "openai/o3": 0.29}
    assert payload["cost_incomplete"] is False
    assert payload["cost_completeness_rate"] == 1.0
    # Deliverables come from a filesystem probe — only files actually on
    # disk appear, keyed by their real filenames.
    assert payload["deliverables"] == {
        "model_patch.diff": True,
        "repro.sh": True,
    }

    # And: invocation_sha256 is a well-formed hex digest.
    assert len(payload["invocation_sha256"]) == 64
    int(payload["invocation_sha256"], 16)  # must be hex


@pytest.mark.asyncio
async def test_write_run_manifest_handles_missing_cost_summary(tmp_path: Path) -> None:
    # Given: a summary without a cost section (e.g., no events yet). This
    # path still represents a successful projection, so summary_available is
    # True and tokens/costs_by_model are present with zero/empty values.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()
    settings = _make_settings()

    # When: writing the manifest.
    path = await persistence.write_run_manifest(
        run_id,
        settings=settings,
        task="empty-run",
        domain_context_path=None,
        exit_status="failed",
        wall_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        wall_ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        summary=ProjectionSummary.empty(),
    )

    # Then: tokens default to zero and costs_by_model is an empty dict.
    payload = json.loads(path.read_text())
    assert payload["summary_available"] is True
    assert payload["tokens"] == {"prompt": 0, "completion": 0}
    assert payload["costs_by_model"] == {}
    assert payload["exit_status"] == "failed"


@pytest.mark.asyncio
async def test_write_run_manifest_none_summary_omits_token_fields(tmp_path: Path) -> None:
    # Given: a projection that could not be computed (e.g., transient
    # Postgres failure). The caller passes summary=None.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()
    settings = _make_settings()

    # When: writing the manifest.
    path = await persistence.write_run_manifest(
        run_id,
        settings=settings,
        task="projection-failed",
        domain_context_path=None,
        exit_status="success",
        wall_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        wall_ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        summary=None,
    )

    # Then: summary_available is false, and tokens/costs_by_model are not
    # written at all — consumers can distinguish "projection failed" from
    # "genuinely zero tokens".
    payload = json.loads(path.read_text())
    assert payload["summary_available"] is False
    assert "tokens" not in payload
    assert "costs_by_model" not in payload


@pytest.mark.asyncio
async def test_write_run_manifest_probes_filesystem_for_deliverables(
    tmp_path: Path,
) -> None:
    # Given: a run directory whose testcase/ carries arbitrary files not
    # listed anywhere in presentation code. The filesystem probe must
    # surface whatever is actually on disk.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()
    testcase = tmp_path / str(run_id) / "testcase"
    testcase.mkdir(parents=True)
    (testcase / "custom_artifact.tar.gz").write_bytes(b"payload")
    (testcase / "scratch.txt").write_text("note\n")
    # Nested directories are NOT flattened into the top-level list.
    nested = testcase / "nested"
    nested.mkdir()
    (nested / "buried.log").write_text("hidden\n")

    # When: writing the manifest.
    path = await persistence.write_run_manifest(
        run_id,
        settings=_make_settings(),
        task="custom",
        domain_context_path=None,
        exit_status="success",
        wall_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        wall_ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        summary=ProjectionSummary.empty(),
    )

    # Then: only the top-level files show up, each with value True.
    payload = json.loads(path.read_text())
    assert payload["deliverables"] == {
        "custom_artifact.tar.gz": True,
        "scratch.txt": True,
    }


@pytest.mark.asyncio
async def test_write_run_manifest_empty_deliverables_when_no_testcase_dir(
    tmp_path: Path,
) -> None:
    # Given: a run that never produced a testcase directory.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()

    # When
    path = await persistence.write_run_manifest(
        run_id,
        settings=_make_settings(),
        task="no-testcase",
        domain_context_path=None,
        exit_status="failed",
        wall_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        wall_ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        summary=ProjectionSummary.empty(),
    )

    # Then: deliverables is an empty dict — no hardcoded filenames appear.
    payload = json.loads(path.read_text())
    assert payload["deliverables"] == {}


@pytest.mark.asyncio
async def test_write_run_manifest_records_provenance_fields(tmp_path: Path) -> None:
    """PR 6: manifest carries uv_lock_sha256 and effective_config_path so a run
    can be tied back to its dependency graph and per-run config snapshot."""
    # Given: persistence pointed at a tmp runs dir.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()

    # When: writing the manifest.
    path = await persistence.write_run_manifest(
        run_id,
        settings=_make_settings(),
        task="task",
        domain_context_path=None,
        exit_status="success",
        wall_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        wall_ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        summary=ProjectionSummary.empty(),
    )

    # Then: the manifest carries both provenance fields.
    payload = json.loads(path.read_text())
    # uv_lock_sha256 is the sha256 of the repo's uv.lock when present;
    # otherwise None. The repo under test has uv.lock so we expect a hex digest.
    uv_lock_sha = payload["uv_lock_sha256"]
    assert uv_lock_sha is None or len(uv_lock_sha) == 64
    if uv_lock_sha is not None:
        int(uv_lock_sha, 16)  # must be hex
    # effective_config_path is documented relative location of the snapshot.
    assert payload["effective_config_path"] == "effective_config.yaml"
    assert len(payload["config_hash"]) == 64
    assert payload["treatment_version"] is None


@pytest.mark.asyncio
async def test_write_run_manifest_is_atomic_on_rewrite(tmp_path: Path) -> None:
    # Given: an existing manifest with stale content.
    persistence = RunPersistence(tmp_path)
    run_id = uuid4()
    run_dir = tmp_path / str(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text('{"stale": true}')

    settings = _make_settings()

    # When: re-writing.
    await persistence.write_run_manifest(
        run_id,
        settings=settings,
        task="task",
        domain_context_path=None,
        exit_status="success",
        wall_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        wall_ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        summary=ProjectionSummary.empty(),
    )

    # Then: no leftover temp files and the manifest is fully replaced.
    manifest = run_dir / "run_manifest.json"
    payload = json.loads(manifest.read_text())
    assert payload["run_id"] == str(run_id)
    assert "stale" not in payload
    leftover = [p for p in run_dir.iterdir() if p.name != "run_manifest.json"]
    assert leftover == []
