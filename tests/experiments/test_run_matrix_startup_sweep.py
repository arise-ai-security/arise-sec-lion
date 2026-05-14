"""Unit tests for the ``run_matrix`` startup sweep (Phase 4 F.2).

The sweep removes stale worker/seed containers (filtered by the
``arise.session_pid`` label) and orphan ``.run-result-*.json`` files
left behind by previously hard-killed matrix runs. These tests cover
both arms without touching a real Docker daemon: ``subprocess.run`` is
monkey-patched at the module level.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from experiments.shared.scripts import run_matrix


if TYPE_CHECKING:
    import pytest


_DEAD_PID = 2**31 - 1  # Max int32; PID kernel does not allocate.


def _ps_result(stdout: str) -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` mimicking ``docker ps`` stdout."""
    return subprocess.CompletedProcess(
        args=["docker", "ps"], returncode=0, stdout=stdout, stderr=""
    )


def _rm_result() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker", "rm"], returncode=0, stdout="", stderr=""
    )


def test_sweep_removes_containers_with_dead_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-PID containers are removed via a follow-up ``docker rm -f`` call."""

    # Given: docker ps returns one row whose owning PID is no longer alive.
    stdout = f"abc123\t{_DEAD_PID}\n"
    mock_run = MagicMock(side_effect=[_ps_result(stdout), _rm_result()])
    monkeypatch.setattr(run_matrix.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(run_matrix.subprocess, "run", mock_run)

    # When: the sweep runs.
    run_matrix._sweep_stale_containers()

    # Then: two subprocess.run calls — docker ps, then docker rm -f abc123.
    assert mock_run.call_count == 2
    first_call_args = mock_run.call_args_list[0].args[0]
    assert first_call_args[:3] == ["docker", "ps", "-a"]
    second_call_args = mock_run.call_args_list[1].args[0]
    assert second_call_args == ["docker", "rm", "-f", "abc123"]


def test_sweep_keeps_containers_with_live_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live-PID containers are left untouched — no ``docker rm`` is issued."""

    # Given: docker ps returns one row whose owning PID is THIS pytest process.
    stdout = f"def456\t{os.getpid()}\n"
    mock_run = MagicMock(side_effect=[_ps_result(stdout)])
    monkeypatch.setattr(run_matrix.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(run_matrix.subprocess, "run", mock_run)

    # When: the sweep runs.
    run_matrix._sweep_stale_containers()

    # Then: only the docker ps probe ran; no docker rm follow-up.
    assert mock_run.call_count == 1
    first_call_args = mock_run.call_args_list[0].args[0]
    assert first_call_args[:3] == ["docker", "ps", "-a"]


def test_sweep_unlinks_orphan_result_files_older_than_1h(tmp_path: Path) -> None:
    """``.run-result-*.json`` files older than the cutoff are unlinked."""

    # Given: an orphan result file with an mtime well before the 1h cutoff.
    pool = tmp_path
    orphan = pool / ".run-result-deadbeef.json"
    orphan.write_text("{}", encoding="utf-8")
    cutoff = time.time() - run_matrix._ORPHAN_RESULT_MAX_AGE_SECONDS
    os.utime(orphan, (cutoff - 1, cutoff - 1))

    # When: the per-pool orphan sweep runs against this directory.
    run_matrix._sweep_orphan_result_files(pool)

    # Then: the orphan file has been removed.
    assert not orphan.exists()


def test_sweep_preserves_recent_result_files(tmp_path: Path) -> None:
    """Fresh ``.run-result-*.json`` files (mtime within 1h) are preserved."""

    # Given: a result file written just now (well inside the cutoff).
    pool = tmp_path
    fresh = pool / ".run-result-cafef00d.json"
    fresh.write_text("{}", encoding="utf-8")

    # When: the per-pool orphan sweep runs.
    run_matrix._sweep_orphan_result_files(pool)

    # Then: the fresh file is still on disk.
    assert fresh.exists()
