"""Regression tests for the `run_arise` harness.

Covers the N-4 fix (audit §7.5.5): each `main.py run` subprocess receives a
unique per-invocation result file via `ARISE_RUN_RESULT_PATH` so concurrent
matrix dispatches cannot misattribute each other's `run_id`. Replaces the
old `.last_run.json` pointer-based discovery.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from experiments.shared import harness
from experiments.shared.scripts import _paths


logger = logging.getLogger(__name__)


def _write_study_layout(tmp_path: Path, *, task: str, cell: str) -> Path:
    """Provision the minimum study layout `run_arise` reads before invoking the subprocess."""
    study_dir = tmp_path / "experiments" / "2026-test-study"
    study_dir.mkdir(parents=True)
    context_file = study_dir / f"{task}.json"
    context_file.write_text("{}", encoding="utf-8")
    dataset_path = study_dir / "dataset.yaml"
    dataset_path.write_text(
        yaml.safe_dump(
            {
                "default_cves": [task],
                "source": {"paths": [str(context_file.relative_to(tmp_path))]},
            }
        ),
        encoding="utf-8",
    )
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": "2026-test-study",
                "dataset": "dataset.yaml",
                "cells": {cell: {"runner": "arise"}},
            }
        ),
        encoding="utf-8",
    )
    return study_dir


@pytest.fixture
def study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Provision a fake study with REPO_ROOT pointed at tmp_path."""
    monkeypatch.setattr(_paths, "REPO_ROOT", tmp_path)
    # `harness` and `register_run` import `get_repo_root` from their own modules;
    # patch each call site so the redirection takes effect everywhere.
    monkeypatch.setattr(harness, "get_repo_root", lambda: tmp_path)
    from experiments.shared.scripts import register_run as register_run_module

    monkeypatch.setattr(register_run_module, "get_repo_root", lambda: tmp_path)
    task = "fake-pkg.cve-2099-0001"
    cell = "A1"
    _write_study_layout(tmp_path, task=task, cell=cell)
    config = tmp_path / "experiments" / "2026-test-study" / "configs" / "A1.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("output:\n  directory: runs\n", encoding="utf-8")
    return {"study_id": "2026-test-study", "task": task, "cell": cell, "config": config}


def _stub_project_events_to_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(**_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(harness, "project_events_to_jsonl", _noop)


def _stub_register_run(monkeypatch: pytest.MonkeyPatch, captured: list[UUID]) -> None:
    def _capture(*, run_id: UUID, **_kwargs: Any) -> Path:
        captured.append(run_id)
        return Path("/dev/null")

    monkeypatch.setattr(harness, "register_run", _capture)


def test_run_arise_reads_run_id_from_per_invocation_result_file(
    study: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a stubbed subprocess that writes a known boss_id to the env-var path
    expected = uuid4()

    def fake_invoke(*, result_path: Path, **_kwargs: Any) -> int:
        result_path.write_text(
            json.dumps({"boss_id": str(expected), "status": "success"}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(harness, "_invoke_main_py", fake_invoke)
    _stub_project_events_to_jsonl(monkeypatch)
    captured: list[UUID] = []
    _stub_register_run(monkeypatch, captured)

    # When: run_arise executes
    returned = harness.run_arise(
        study_id=study["study_id"],
        cell=study["cell"],
        task=study["task"],
        replicate=0,
        config=study["config"],
    )

    # Then: the returned run_id matches what the subprocess wrote
    assert returned == expected
    assert captured == [expected]


def test_run_arise_raises_when_subprocess_does_not_write_result_file(
    study: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a stubbed subprocess that exits without writing the result file
    def fake_invoke(**_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(harness, "_invoke_main_py", fake_invoke)
    _stub_project_events_to_jsonl(monkeypatch)
    _stub_register_run(monkeypatch, [])

    # When/Then: run_arise refuses to enroll
    with pytest.raises(FileNotFoundError, match="run-result"):
        harness.run_arise(
            study_id=study["study_id"],
            cell=study["cell"],
            task=study["task"],
            replicate=0,
            config=study["config"],
        )


def test_run_arise_concurrent_invocations_do_not_misattribute_run_ids(
    study: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: each thread writes its own UUID to its own result path; if the
    # harness ever read from a shared pointer, threads would race and the
    # captured UUIDs would not match the ones each thread wrote.
    barrier = threading.Barrier(4)
    seen: dict[int, tuple[UUID, UUID]] = {}

    def fake_invoke(*, result_path: Path, **_kwargs: Any) -> int:
        my_uuid = uuid4()
        # Synchronise so all threads write at roughly the same time, maximising
        # any race window if one existed.
        barrier.wait()
        result_path.write_text(
            json.dumps({"boss_id": str(my_uuid), "status": "success"}),
            encoding="utf-8",
        )
        # Stash the UUID this thread WROTE so the assertion can compare it
        # against what `run_arise` returned for the same thread.
        seen[threading.get_ident()] = (my_uuid, my_uuid)
        return 0

    monkeypatch.setattr(harness, "_invoke_main_py", fake_invoke)
    _stub_project_events_to_jsonl(monkeypatch)
    _stub_register_run(monkeypatch, [])

    results: dict[int, UUID] = {}

    def worker() -> None:
        returned = harness.run_arise(
            study_id=study["study_id"],
            cell=study["cell"],
            task=study["task"],
            replicate=0,
            config=study["config"],
        )
        results[threading.get_ident()] = returned

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Then: every thread saw its own UUID, not another thread's
    assert len(results) == 4
    for tid, returned in results.items():
        wrote, _ = seen[tid]
        assert returned == wrote, (
            f"thread {tid} wrote {wrote} but run_arise returned {returned}"
        )
