"""OS-level reaping of OpenHands MCP stdio child processes.

The SDK's ``Conversation.close()`` is best-effort about its MCP children;
this module owns the SIGTERM -> waitpid-poll -> SIGKILL escalation that
keeps long matrix runs free of zombies and orphaned children.
"""

import logging
import os
import signal
import subprocess
import time as _time

from infrastructure.cleanup.registry import pid_alive


logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 5


def snapshot_child_pids() -> set[int]:
    """Return the set of direct child PIDs of this process.

    Used to identify MCP stdio subprocesses spawned while OpenHands initializes
    and runs a conversation. OpenHands lazy-loads MCP tools during
    ``send_message()`` / ``run()``, so the caller snapshots before building the
    conversation and diffs against the current children during cleanup.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv is fully owned here.
            ["pgrep", "-P", str(os.getpid())],  # noqa: S607 - intentionally PATH-based.
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode not in (0, 1):
        return set()
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            pids.add(int(token))
        except ValueError:
            continue
    return pids


def reap_with_escalation(  # noqa: PLR0912 - explicit OS cleanup branches.
    pids: set[int],
    *,
    grace_seconds: float = float(SHUTDOWN_GRACE_SECONDS),
    poll_interval: float = 0.1,
) -> None:
    """SIGTERM ``pids``, poll ``waitpid``, then SIGKILL survivors.

    Codex review #2: the previous reaper sent SIGTERM and returned
    without reaping. SDK-spawned MCP children that exited stayed as
    zombies, and children that ignored SIGTERM stayed live. Both
    accumulate across long matrix runs.

    Steps:
      1. SIGTERM every still-alive PID (uses ``pid_alive`` to skip
         ones the SDK already cleaned up).
      2. Poll ``os.waitpid(pid, WNOHANG)`` every ``poll_interval``
         seconds until either every PID is reaped or
         ``grace_seconds`` elapse. ``waitpid`` returns ``(0, 0)``
         when the child is still running; ``(pid, _)`` once reaped;
         raises ``ChildProcessError`` if the OS already reaped it.
      3. For any PID still alive after the grace, SIGKILL and drain
         ``waitpid`` again (best-effort).
      4. Treat ``ChildProcessError`` and ``ProcessLookupError`` as
         "already gone." All other ``OSError`` is logged at debug.
    """
    if not pids:
        return

    # Step 1: SIGTERM.
    for pid in list(pids):
        if not pid_alive(pid):
            pids.discard(pid)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pids.discard(pid)
        except OSError:
            logger.debug("SIGTERM to MCP child PID %d failed", pid, exc_info=True)

    # Step 2: poll waitpid until grace expires.
    deadline = _time.monotonic() + grace_seconds
    while pids and _time.monotonic() < deadline:
        _drain_waitpid(pids)
        if pids:
            _time.sleep(poll_interval)

    if not pids:
        return

    # Step 3: SIGKILL survivors.
    for pid in list(pids):
        if not pid_alive(pid):
            pids.discard(pid)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pids.discard(pid)
        except OSError:
            logger.debug("SIGKILL to MCP child PID %d failed", pid, exc_info=True)

    # Step 4: drain waitpid one more time so SIGKILLed children don't
    # linger as zombies waiting for the parent to reap them.
    kill_deadline = _time.monotonic() + grace_seconds
    while pids and _time.monotonic() < kill_deadline:
        _drain_waitpid(pids)
        if pids:
            _time.sleep(poll_interval)


def _drain_waitpid(pids: set[int]) -> None:
    """Non-blocking ``waitpid`` for each PID; discard those reaped."""
    for pid in list(pids):
        try:
            wpid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # The OS already reaped this child (e.g., handled by
            # another waiter or by ``signal.SIGCHLD`` default).
            pids.discard(pid)
            continue
        except OSError:
            logger.debug("waitpid for MCP child PID %d failed", pid, exc_info=True)
            continue
        if wpid == pid:
            pids.discard(pid)
