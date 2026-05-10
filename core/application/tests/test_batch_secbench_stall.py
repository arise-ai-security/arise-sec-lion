"""Tests for batch SEC-bench stall detection safeguards."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import random
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


@pytest.mark.asyncio
async def test_run_instance_attempts_child_rescue_before_stall_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Pre-kill rescue should target stale analyzing child before run kill."""
    batch = _load_batch_module()

    instance_id = "demo.cve-0000-0099"
    fixture = tmp_path / f"{instance_id}.json"
    fixture.write_text(
        (
            "{\n"
            '  "project_name": "demo",\n'
            '  "bug_description": "child analyzing forever"\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(batch, "FIXTURE_DIR", tmp_path)
    monkeypatch.setattr(batch, "DECOMP_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "STALL_SEC", 99999)  # avoid run kill path in this test
    monkeypatch.setattr(batch, "CHILD_RESCUE_SEC", 1)
    monkeypatch.setattr(batch, "CHILD_RESCUE_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(batch, "RUN_TIMEOUT_SEC", 99999)

    class _Stdout:
        async def read(self, _n: int) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = _Stdout()
            self._ticks = 0

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    proc = _Proc()

    async def _fake_spawn(*_args, **_kwargs):
        return proc

    async def _fake_sleep(_secs: float) -> None:
        proc._ticks += 1
        # Exit after giving rescue path one loop to run.
        if proc._ticks >= 2:
            proc.returncode = 0
        return

    rescued: list[str] = []

    async def _fake_rescue(*, instance_id: str, agent_id: str, stale_seconds: float, last_event_type: str) -> bool:
        rescued.append(agent_id)
        return True

    async def _db_boss_id(_iid: str, _after_ts: str):
        return "boss-xyz"

    async def _db_agent_count(_boss_id: str):
        return 4

    async def _db_boss_finished_spawning(_boss_id: str):
        return False

    async def _db_boss_manager_count(_boss_id: str):
        return (0, 0)

    async def _db_last_event_snapshot(_boss_id: str):
        return (2.5, "child-stale", "AgentExecutionStarted")

    async def _db_stale_analyzing_children(_boss_id: str, stale_seconds: float, limit: int = 5):
        assert stale_seconds == 1
        assert limit == 5
        return [("child-stale-uuid", 1200.0, "AgentExecutionStarted")]

    async def _db_run_completed(_boss_id: str):
        return "success"

    monkeypatch.setattr(batch.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(batch.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(batch, "_rescue_stale_child_retry", _fake_rescue)
    monkeypatch.setattr(batch, "db_boss_id", _db_boss_id)
    monkeypatch.setattr(batch, "db_agent_count", _db_agent_count)
    monkeypatch.setattr(batch, "db_boss_finished_spawning", _db_boss_finished_spawning)
    monkeypatch.setattr(batch, "db_boss_manager_count", _db_boss_manager_count)
    monkeypatch.setattr(batch, "db_last_event_snapshot", _db_last_event_snapshot)
    monkeypatch.setattr(batch, "db_stale_analyzing_children", _db_stale_analyzing_children)
    monkeypatch.setattr(batch, "db_run_completed", _db_run_completed)

    result = await batch.run_instance(instance_id, attempt=1)

    assert result.status == "success"
    assert rescued == ["child-stale-uuid"]


@pytest.mark.asyncio
async def test_rescue_child_retry_treats_zero_exit_code_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: subprocess returncode=0 must not be interpreted as failure."""
    batch = _load_batch_module()

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"Rescued agent ...", b"")

    async def _fake_spawn(*_args, **_kwargs):
        return _Proc()

    monkeypatch.setattr(batch.asyncio, "create_subprocess_exec", _fake_spawn)
    ok = await batch._rescue_stale_child_retry(
        instance_id="demo.cve-1234-0001",
        agent_id="11111111-2222-3333-4444-555555555555",
        stale_seconds=777.0,
        last_event_type="RetryScheduled",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_run_instance_abandons_child_after_rescue_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If rescue keeps failing, batch should safe-skip (abandon) the child."""
    batch = _load_batch_module()

    instance_id = "demo.cve-abandon-0001"
    fixture = tmp_path / f"{instance_id}.json"
    fixture.write_text(
        (
            "{\n"
            '  "project_name": "demo",\n'
            '  "bug_description": "rescue budget exhaustion path"\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(batch, "FIXTURE_DIR", tmp_path)
    monkeypatch.setattr(batch, "DECOMP_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "CHILD_RESCUE_SEC", 1)
    monkeypatch.setattr(batch, "CHILD_RESCUE_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "CHILD_RESCUE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(batch, "STALL_SEC", 99999)
    monkeypatch.setattr(batch, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(batch, "RUN_TIMEOUT_SEC", 99999)

    class _Stdout:
        async def read(self, _n: int) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = _Stdout()
            self.ticks = 0

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    proc = _Proc()

    async def _fake_spawn(*_args, **_kwargs):
        return proc

    async def _fake_sleep(_secs: float) -> None:
        proc.ticks += 1
        # Exit once abandon path has had a chance to execute.
        if proc.ticks >= 4:
            proc.returncode = 0
        return

    rescue_calls: list[str] = []
    abandon_calls: list[str] = []

    async def _fake_rescue(*, instance_id: str, agent_id: str, stale_seconds: float, last_event_type: str) -> bool:
        rescue_calls.append(agent_id)
        return False

    async def _fake_abandon(*, instance_id: str, agent_id: str, stale_seconds: float, last_event_type: str) -> bool:
        abandon_calls.append(agent_id)
        return True

    async def _db_boss_id(_iid: str, _after_ts: str):
        return "boss-abandon"

    async def _db_agent_count(_boss_id: str):
        return 6

    async def _db_boss_finished_spawning(_boss_id: str):
        return False

    async def _db_boss_manager_count(_boss_id: str):
        return (0, 0)

    async def _db_last_event_snapshot(_boss_id: str):
        return (2.5, "child-abandon", "RetryScheduled")

    async def _db_stale_analyzing_children(_boss_id: str, stale_seconds: float, limit: int = 5):
        return [("child-abandon-uuid", 1200.0, "RetryScheduled")]

    async def _db_run_completed(_boss_id: str):
        return "success"

    monkeypatch.setattr(batch.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(batch.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(batch, "_rescue_stale_child_retry", _fake_rescue)
    monkeypatch.setattr(batch, "_abandon_stale_child", _fake_abandon)
    monkeypatch.setattr(batch, "db_boss_id", _db_boss_id)
    monkeypatch.setattr(batch, "db_agent_count", _db_agent_count)
    monkeypatch.setattr(batch, "db_boss_finished_spawning", _db_boss_finished_spawning)
    monkeypatch.setattr(batch, "db_boss_manager_count", _db_boss_manager_count)
    monkeypatch.setattr(batch, "db_last_event_snapshot", _db_last_event_snapshot)
    monkeypatch.setattr(batch, "db_stale_analyzing_children", _db_stale_analyzing_children)
    monkeypatch.setattr(batch, "db_run_completed", _db_run_completed)

    result = await batch.run_instance(instance_id, attempt=1)

    assert result.status == "success"
    assert rescue_calls == ["child-abandon-uuid", "child-abandon-uuid"]
    assert abandon_calls == ["child-abandon-uuid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [7, 11, 19, 23, 31, 47, 59, 71])
async def test_run_instance_randomized_byzantine_subprocess_lifecycle_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seed: int,
) -> None:
    """Randomized batch lifecycle fuzz for subprocess + rescue behavior.

    Simulates flaky rescue outcomes and stale analyzing children while the
    run subprocess remains alive. The invariant is bounded termination:
    ``run_instance`` must return (success/failed/etc.) rather than loop forever.
    """
    batch = _load_batch_module()
    rng = random.Random(seed)

    instance_id = f"demo.cve-rand-{seed}"
    fixture = tmp_path / f"{instance_id}.json"
    fixture.write_text(
        (
            "{\n"
            '  "project_name": "demo",\n'
            '  "bug_description": "randomized byzantine subprocess lifecycle"\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(batch, "FIXTURE_DIR", tmp_path)
    monkeypatch.setattr(batch, "DECOMP_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "CHILD_RESCUE_SEC", 2)
    monkeypatch.setattr(batch, "CHILD_RESCUE_GRACE_SEC", 0)
    monkeypatch.setattr(batch, "STALL_SEC", 6)
    monkeypatch.setattr(batch, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(batch, "RUN_TIMEOUT_SEC", 99999)

    class _Stdout:
        async def read(self, _n: int) -> bytes:
            return b""

    class _Proc:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = _Stdout()
            self.ticks = 0

        async def wait(self) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            if self.returncode is None:
                self.returncode = 0
            return (b"ok", b"")

    proc = _Proc()

    async def _fake_spawn(*_args, **_kwargs):
        return proc

    async def _fake_sleep(_secs: float) -> None:
        proc.ticks += 1
        # Optional "natural" completion branch for some seeds.
        if proc.ticks == 4 and rng.random() < 0.25:
            proc.returncode = 0
        return

    kill_calls: list[tuple[str, str | None]] = []

    async def _fake_kill(_proc, iid: str, boss_id: str | None) -> None:
        kill_calls.append((iid, boss_id))
        _proc.returncode = -9

    rescue_calls: list[str] = []
    rescue_successes: list[str] = []
    rescue_outcomes: list[bool] = []
    completed_status: str | None = None

    async def _fake_rescue(
        *,
        instance_id: str,
        agent_id: str,
        stale_seconds: float,
        last_event_type: str,
    ) -> bool:
        nonlocal completed_status
        rescue_calls.append(agent_id)
        # Flaky subprocess-like outcome.
        ok = rng.random() < 0.55
        rescue_outcomes.append(ok)
        if ok:
            rescue_successes.append(agent_id)
            # Some successes actually recover and let run complete.
            if rng.random() < 0.35:
                proc.returncode = 0
                completed_status = "success"
        return ok

    async def _db_boss_id(_iid: str, _after_ts: str):
        return "boss-rand"

    async def _db_agent_count(_boss_id: str):
        return 12

    async def _db_boss_finished_spawning(_boss_id: str):
        return False

    async def _db_boss_manager_count(_boss_id: str):
        return (0, 0)

    async def _db_last_event_snapshot(_boss_id: str):
        # Event stream looks stale and worsening over time.
        return (float(proc.ticks + 2), "child-rand", "RetryScheduled")

    async def _db_stale_analyzing_children(_boss_id: str, stale_seconds: float, limit: int = 5):
        assert stale_seconds == 2
        assert limit == 5
        return [("child-rand-uuid", float(proc.ticks + 2), "RetryScheduled")]

    async def _db_run_completed(_boss_id: str):
        return completed_status

    monkeypatch.setattr(batch.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(batch.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(batch, "_kill_run", _fake_kill)
    monkeypatch.setattr(batch, "_rescue_stale_child_retry", _fake_rescue)
    monkeypatch.setattr(batch, "db_boss_id", _db_boss_id)
    monkeypatch.setattr(batch, "db_agent_count", _db_agent_count)
    monkeypatch.setattr(batch, "db_boss_finished_spawning", _db_boss_finished_spawning)
    monkeypatch.setattr(batch, "db_boss_manager_count", _db_boss_manager_count)
    monkeypatch.setattr(batch, "db_last_event_snapshot", _db_last_event_snapshot)
    monkeypatch.setattr(batch, "db_stale_analyzing_children", _db_stale_analyzing_children)
    monkeypatch.setattr(batch, "db_run_completed", _db_run_completed)

    result = await asyncio.wait_for(batch.run_instance(instance_id, attempt=1), timeout=2)

    # Bounded termination invariant
    assert result.status in {"success", "failed", "under_decomposed", "timeout", "error"}
    # Dedupe invariant: repeated attempts are allowed before first success,
    # but after first success no further rescue calls should happen.
    if rescue_successes:
        first_success_idx = rescue_outcomes.index(True)
        assert rescue_calls.count("child-rand-uuid") == first_success_idx + 1
    # If run ended via stall kill, kill path should be exercised.
    if result.status == "failed" and "stalled" in (result.error_message or ""):
        assert kill_calls == [(instance_id, "boss-rand")]


@pytest.mark.asyncio
async def test_db_stale_analyzing_children_includes_agent_execution_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: stale-child query must include AgentExecutionStarted.

    A worker can be stuck right after execution starts, before any
    StatusChanged/RetryScheduled event appears.
    """
    batch = _load_batch_module()

    captured_sql: dict[str, str] = {}

    async def _fake_psql(sql: str) -> str:
        captured_sql["text"] = sql
        return "child-abc|901.5|AgentExecutionStarted"

    monkeypatch.setattr(batch, "_psql", _fake_psql)

    rows = await batch.db_stale_analyzing_children(
        "00000000-0000-0000-0000-000000000001",
        stale_seconds=600,
        limit=5,
    )

    assert rows == [("child-abc", 901.5, "AgentExecutionStarted")]
    assert "AgentExecutionStarted" in captured_sql["text"]
