"""Tests for batch SEC-bench stall detection safeguards."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest


def _load_batch_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "batch_secbench.py"
    spec = importlib.util.spec_from_file_location("batch_secbench", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_run_instance_kills_alive_process_when_events_stall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If process is alive but hierarchy events are stale, run is killed as failed.

    This captures the exact failure mode seen in production:
    - subprocess still running
    - no new DB events for extended period
    """
    batch = _load_batch_module()

    instance_id = "demo.cve-0000-0000"
    fixture = tmp_path / f"{instance_id}.json"
    fixture.write_text(
        (
            "{\n"
            '  "project_name": "demo",\n'
            '  "bug_description": "repro hangs forever"\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(batch, "FIXTURE_DIR", tmp_path)
    monkeypatch.setattr(batch, "DECOMP_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "STALL_SEC", 1)
    monkeypatch.setattr(batch, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(batch, "RUN_TIMEOUT_SEC", 99999)

    class _Stdout:
        async def read(self, _n: int) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = _Stdout()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    proc = _Proc()

    async def _fake_spawn(*_args, **_kwargs):
        return proc

    killed: list[tuple[str, str | None]] = []

    async def _fake_kill(_proc, iid: str, boss_id: str | None) -> None:
        killed.append((iid, boss_id))
        _proc.returncode = -9

    async def _fake_sleep(_secs: float) -> None:
        return

    monkeypatch.setattr(batch.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(batch.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(batch, "_kill_run", _fake_kill)

    async def _db_boss_id(_iid: str, _after_ts: str):
        return "boss-123"

    async def _db_agent_count(_boss_id: str):
        return 4

    async def _db_boss_finished_spawning(_boss_id: str):
        return False

    async def _db_boss_manager_count(_boss_id: str):
        return (0, 0)

    async def _db_last_event_snapshot(_boss_id: str):
        return (2.5, "worker-abc", "AgentExecutionStarted")

    async def _db_run_completed(_boss_id: str):
        return None

    monkeypatch.setattr(batch, "db_boss_id", _db_boss_id)
    monkeypatch.setattr(batch, "db_agent_count", _db_agent_count)
    monkeypatch.setattr(batch, "db_boss_finished_spawning", _db_boss_finished_spawning)
    monkeypatch.setattr(batch, "db_boss_manager_count", _db_boss_manager_count)
    monkeypatch.setattr(batch, "db_last_event_snapshot", _db_last_event_snapshot)
    monkeypatch.setattr(batch, "db_run_completed", _db_run_completed)

    result = await batch.run_instance(instance_id, attempt=1)

    assert result.status == "failed"
    assert "stalled" in result.error_message
    assert killed == [(instance_id, "boss-123")]


@pytest.mark.asyncio
async def test_run_with_retry_retries_stalled_failure_even_if_early_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stall signatures are retried even when duration is below early-death cutoff."""
    batch = _load_batch_module()

    calls: list[int] = []

    async def _build_image(_iid: str) -> bool:
        return True

    async def _remove_image(_iid: str) -> None:
        return

    def _upsert_csv(_result) -> None:
        return

    async def _sleep(_secs: float) -> None:
        return

    async def _run_instance(_iid: str, attempt: int):
        calls.append(attempt)
        if attempt == 1:
            return batch.RunResult(
                instance_id="demo.cve-0000-0001",
                status="failed",
                duration_seconds=120.0,  # early-death bucket
                error_message="stalled 120s",
            )
        return batch.RunResult(
            instance_id="demo.cve-0000-0001",
            status="success",
            duration_seconds=130.0,
        )

    monkeypatch.setattr(batch, "build_image", _build_image)
    monkeypatch.setattr(batch, "_remove_image", _remove_image)
    monkeypatch.setattr(batch, "upsert_csv", _upsert_csv)
    monkeypatch.setattr(batch, "run_instance", _run_instance)
    monkeypatch.setattr(batch.asyncio, "sleep", _sleep)

    sem = asyncio.Semaphore(1)
    result = await batch._run_with_retry(
        "demo.cve-0000-0001", sem, max_retries=2, stagger_delay=0.0,
    )

    assert result.status == "success"
    assert result.tries == 2
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_run_with_retry_retries_timeout_even_if_early_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout signatures are treated as hang-like and retried."""
    batch = _load_batch_module()

    calls: list[int] = []

    async def _build_image(_iid: str) -> bool:
        return True

    async def _remove_image(_iid: str) -> None:
        return

    def _upsert_csv(_result) -> None:
        return

    async def _sleep(_secs: float) -> None:
        return

    async def _run_instance(_iid: str, attempt: int):
        calls.append(attempt)
        if attempt == 1:
            return batch.RunResult(
                instance_id="demo.cve-0000-0002",
                status="timeout",
                duration_seconds=200.0,  # early-death bucket
                error_message="run timeout",
            )
        return batch.RunResult(
            instance_id="demo.cve-0000-0002",
            status="failed",
            duration_seconds=700.0,
            error_message="non-timeout failure after retry",
        )

    monkeypatch.setattr(batch, "build_image", _build_image)
    monkeypatch.setattr(batch, "_remove_image", _remove_image)
    monkeypatch.setattr(batch, "upsert_csv", _upsert_csv)
    monkeypatch.setattr(batch, "run_instance", _run_instance)
    monkeypatch.setattr(batch.asyncio, "sleep", _sleep)

    sem = asyncio.Semaphore(1)
    result = await batch._run_with_retry(
        "demo.cve-0000-0002", sem, max_retries=2, stagger_delay=0.0,
    )

    assert result.tries == 2
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_run_with_retry_still_skips_non_hang_early_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: non-hang early failures remain non-retryable."""
    batch = _load_batch_module()

    calls: list[int] = []

    async def _build_image(_iid: str) -> bool:
        return True

    async def _remove_image(_iid: str) -> None:
        return

    def _upsert_csv(_result) -> None:
        return

    async def _run_instance(_iid: str, attempt: int):
        calls.append(attempt)
        return batch.RunResult(
            instance_id="demo.cve-0000-0003",
            status="failed",
            duration_seconds=120.0,
            error_message="exit_code=1",
        )

    monkeypatch.setattr(batch, "build_image", _build_image)
    monkeypatch.setattr(batch, "_remove_image", _remove_image)
    monkeypatch.setattr(batch, "upsert_csv", _upsert_csv)
    monkeypatch.setattr(batch, "run_instance", _run_instance)

    sem = asyncio.Semaphore(1)
    result = await batch._run_with_retry(
        "demo.cve-0000-0003", sem, max_retries=3, stagger_delay=0.0,
    )

    assert result.tries == 1
    assert calls == [1]
