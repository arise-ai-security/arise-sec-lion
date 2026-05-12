"""Tests for :mod:`infrastructure.cleanup.registry`.

Signal / ``atexit`` tests use real subprocesses so that pytest's own
signal handlers (used by ``-x`` interrupt, timeouts, and the
``faulthandler`` integration) are not corrupted by an in-process
``signal.signal`` call.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from infrastructure.cleanup.registry import CleanupRegistry, _pid_alive


REPO_ROOT = Path(__file__).resolve().parents[2]


# --- in-process registry behaviour ---------------------------------------


def test_register_stores_handler() -> None:
    """Test that a registered handler is invoked by run_all."""

    # Given: a registry with one handler that flips a flag
    registry = CleanupRegistry()
    calls: list[str] = []
    registry.register("only", lambda: calls.append("only"))

    # When: run_all fires
    registry.run_all()

    # Then: the handler was invoked exactly once
    assert calls == ["only"]


def test_register_idempotent_on_name() -> None:
    """Test that re-registering the same name replaces (not duplicates) the handler."""

    # Given: a registry with two registrations under one name
    registry = CleanupRegistry()
    calls: list[str] = []
    registry.register("x", lambda: calls.append("first"))
    registry.register("x", lambda: calls.append("second"))

    # When: run_all fires
    registry.run_all()

    # Then: only the second registration was invoked
    assert calls == ["second"]


def test_run_all_invokes_in_registration_order() -> None:
    """Test that handlers fire in the order they were registered."""

    # Given: three handlers registered A, B, C
    registry = CleanupRegistry()
    order: list[str] = []
    registry.register("a", lambda: order.append("a"))
    registry.register("b", lambda: order.append("b"))
    registry.register("c", lambda: order.append("c"))

    # When: run_all fires
    registry.run_all()

    # Then: invocation order matches registration order
    assert order == ["a", "b", "c"]


def test_run_all_continues_after_handler_exception() -> None:
    """Test that one handler raising does not block the rest."""

    # Given: a raising handler followed by a recording handler
    registry = CleanupRegistry()
    later_ran: list[bool] = []

    def boom() -> None:
        raise RuntimeError("boom")

    registry.register("boom", boom)
    registry.register("later", lambda: later_ran.append(True))

    # When: run_all fires
    registry.run_all()

    # Then: the second handler still ran
    assert later_ran == [True]


def test_run_all_is_idempotent_after_first_call() -> None:
    """Test that a second run_all is a no-op after the first sweep."""

    # Given: a counter-incrementing handler
    registry = CleanupRegistry()
    counter = {"n": 0}

    def bump() -> None:
        counter["n"] += 1

    registry.register("bump", bump)

    # When: run_all fires twice
    registry.run_all()
    registry.run_all()

    # Then: the counter only advanced once
    assert counter["n"] == 1


# --- _pid_alive ----------------------------------------------------------


def test_pid_alive_returns_true_for_self() -> None:
    """Test that the current process is considered alive."""

    # Given/When: probe own PID
    alive = _pid_alive(os.getpid())

    # Then: alive
    assert alive is True


def test_pid_alive_returns_false_for_definitely_dead_pid() -> None:
    """Test that an out-of-range PID is reported as not alive."""

    # Given/When: probe a PID outside the kernel's allocation range
    alive = _pid_alive(2**31 - 1)

    # Then: not alive
    assert alive is False


# --- signal / atexit subprocess tests ------------------------------------


def _allocate_sentinel_path() -> Path:
    """Return a unique file path that does NOT yet exist."""

    fd, path = tempfile.mkstemp(prefix="cleanup-sentinel-", suffix=".flag")
    os.close(fd)
    os.unlink(path)
    return Path(path)


def _spawn_child(script: str, sentinel: Path) -> subprocess.Popen[bytes]:
    """Launch ``script`` as a child process from the repo root."""

    return subprocess.Popen(
        [sys.executable, "-c", script, str(sentinel)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_or_fail(proc: subprocess.Popen[bytes], reason: str, timeout: float = 5.0) -> None:
    """Wait for ``proc`` to exit; kill and fail if it overruns ``timeout``."""

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)
        pytest.fail(f"child did not exit in {timeout}s: {reason}")


def _wait_for_sentinel_ready(proc: subprocess.Popen[bytes], ready_path: Path) -> None:
    """Spin until the child writes its 'ready' marker or dies."""

    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready_path.exists():
            return
        if proc.poll() is not None:
            stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            pytest.fail(f"child exited before signalling ready: {stderr}")
        time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=5.0)
    pytest.fail("child never signalled ready within 5s")


_SIGNAL_CHILD_SCRIPT = """
import os
import signal
import sys
import time
from pathlib import Path

from infrastructure.cleanup.registry import CleanupRegistry

sentinel = Path(sys.argv[1])
ready = sentinel.with_suffix(".ready")

def _write_sentinel():
    sentinel.write_text("fired")

registry = CleanupRegistry()
registry.register("touch", _write_sentinel)
registry.install()

# Tell the parent we have installed handlers; safe to signal now.
ready.write_text("ready")

# Block until a signal fires.
while True:
    time.sleep(0.1)
"""


_ATEXIT_CHILD_SCRIPT = """
import sys
import time
from pathlib import Path

from infrastructure.cleanup.registry import CleanupRegistry

sentinel = Path(sys.argv[1])

def _write_sentinel():
    sentinel.write_text("fired")

registry = CleanupRegistry()
registry.register("touch", _write_sentinel)
registry.install()

# Brief settle so install() fully completes before interpreter teardown.
time.sleep(0.05)
sys.exit(0)
"""


def test_install_fires_on_sigterm() -> None:
    """Test that SIGTERM causes the registered handler to run before exit."""

    # Given: a child that registers a sentinel handler and pauses
    sentinel = _allocate_sentinel_path()
    ready = sentinel.with_suffix(".ready")
    proc = _spawn_child(_SIGNAL_CHILD_SCRIPT, sentinel)
    try:
        _wait_for_sentinel_ready(proc, ready)

        # When: parent sends SIGTERM
        proc.send_signal(signal.SIGTERM)
        _wait_or_fail(proc, "expected SIGTERM cleanup to run")

        # Then: handler wrote the sentinel file
        assert sentinel.exists(), "SIGTERM did not trigger the cleanup handler"
        assert sentinel.read_text() == "fired"
    finally:
        sentinel.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


def test_install_fires_on_sigint() -> None:
    """Test that SIGINT causes the registered handler to run before exit."""

    # Given: a child that registers a sentinel handler and pauses
    sentinel = _allocate_sentinel_path()
    ready = sentinel.with_suffix(".ready")
    proc = _spawn_child(_SIGNAL_CHILD_SCRIPT, sentinel)
    try:
        _wait_for_sentinel_ready(proc, ready)

        # When: parent sends SIGINT
        proc.send_signal(signal.SIGINT)
        _wait_or_fail(proc, "expected SIGINT cleanup to run")

        # Then: handler wrote the sentinel file
        assert sentinel.exists(), "SIGINT did not trigger the cleanup handler"
        assert sentinel.read_text() == "fired"
    finally:
        sentinel.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


def test_install_fires_atexit_on_normal_exit() -> None:
    """Test that a clean ``sys.exit(0)`` still triggers the cleanup handler."""

    # Given: a child that registers a handler then exits cleanly
    sentinel = _allocate_sentinel_path()
    proc = _spawn_child(_ATEXIT_CHILD_SCRIPT, sentinel)

    try:
        # When: the child runs to completion
        _wait_or_fail(proc, "expected child to exit on its own")

        # Then: handler wrote the sentinel file via atexit
        assert sentinel.exists(), "atexit did not trigger the cleanup handler"
        assert sentinel.read_text() == "fired"
    finally:
        sentinel.unlink(missing_ok=True)
