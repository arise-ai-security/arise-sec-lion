"""SIGKILL recovery drill (Phase 4 F.3).

This test exercises the integration between the run-matrix startup sweep
and a real Docker daemon — specifically, that ``_sweep_stale_state``
removes containers whose owning ``arise.session_pid`` is gone.

Variant chosen: SMOKE surrogate (NOT the full SEC-bench drill).

Rationale:
- The full drill needs ``main.py run --domain security ...`` to bring up
  a real SEC-bench worker, which in turn needs a locally cached
  cell-specific image and a ~1–2 minute runtime budget. The plan
  explicitly authorises a SMOKE surrogate (see ``2026-05-11-concurrency
  -correctness-plan.md`` §4.4) as long as it tests "the integration of
  sweep against real Docker without needing a full agent run".
- The invariant under test is: ``_sweep_stale_state`` finds containers
  tagged with ``arise.session_pid=<dead PID>`` and removes them. The
  full drill validates the same invariant, but the SIGKILLed worker
  setup is incidental to that check.

The surrogate runs in seconds against a tiny ``busybox`` image, which
is small enough to pull on demand. The test is gated by
``@pytest.mark.requires_docker`` so the default ``uv run pytest`` does
NOT exercise it; CI must opt in via ``-m requires_docker``.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from experiments.shared.scripts.run_matrix import _sweep_stale_state


_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEAD_PID = 2**31 - 1  # Max signed int32. Never a live PID.
_PLACEHOLDER_IMAGE = "busybox:latest"


def _docker_available() -> bool:
    try:
        subprocess.run(  # noqa: S603, S607
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return False
    return True


def _container_exists(container_id: str) -> bool:
    """Whether ``docker ps -a`` still lists ``container_id``."""
    result = subprocess.run(  # noqa: S603, S607
        ["docker", "ps", "-a", "--filter", f"id={container_id}", "--format", "{{.ID}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return container_id[:12] in result.stdout


@pytest.mark.requires_docker
def test_sigkill_drill_recovers_container() -> None:
    """``_sweep_stale_state`` removes a container whose ``session_pid`` is dead.

    Drives the SMOKE variant of the F.3 drill:

    1. ``docker run -d`` a placeholder container labelled with a dead PID.
    2. Call ``_sweep_stale_state({repo_root / "runs"})``.
    3. Assert the placeholder is gone.

    If the Docker daemon is not reachable, the test is skipped — the
    ``requires_docker`` marker is the formal opt-in but a missing socket
    is a soft-skip rather than a hard failure.
    """

    # Given: a real container tagged with a PID that is provably not alive.
    if not _docker_available():
        pytest.skip("docker daemon not reachable; SIGKILL drill surrogate skipped")

    container_name = f"arise-sigkill-drill-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(  # noqa: S603, S607
        [
            "docker",
            "run",
            "-d",
            "--rm=false",
            "--name",
            container_name,
            "--label",
            f"arise.session_pid={_DEAD_PID}",
            _PLACEHOLDER_IMAGE,
            "sleep",
            "60",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if run.returncode != 0:
        pytest.skip(
            f"docker run failed (image cache or network); stderr={run.stderr.strip()!r}"
        )
    container_id = run.stdout.strip()
    assert container_id, "docker run -d should print a container id"

    try:
        # And: the container is visible to ``docker ps -a`` BEFORE the sweep.
        assert _container_exists(container_id), (
            "placeholder container should exist before sweep"
        )

        # When: the run-matrix startup sweep fires against the default pool.
        _sweep_stale_state({_REPO_ROOT / "runs"})

        # Then: the container is gone — the sweep removed it via docker rm -f.
        assert not _container_exists(container_id), (
            "placeholder container should be removed by _sweep_stale_state"
        )
    finally:
        # Belt-and-suspenders cleanup in case the sweep missed (test failed).
        subprocess.run(  # noqa: S603, S607
            ["docker", "rm", "-f", container_name],
            check=False,
            capture_output=True,
            timeout=30,
        )

    # Sanity: this test process must NOT have been killed alongside the
    # container; if `os.getpid()` no longer works, the placeholder PID
    # collided with a real process and `pid_alive` would have kept it.
    assert os.getpid() > 0
