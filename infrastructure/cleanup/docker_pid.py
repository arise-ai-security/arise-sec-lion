"""PID-labeled Docker container cleanup for process-teardown paths."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess


logger = logging.getLogger(__name__)


def docker_pid_cleanup() -> None:
    """Remove every container labeled with this process's PID.

    Second line of defense against leaked worker/seed containers. The
    happy-path try/finally in flat-mode + the hierarchical role dispatch
    already call ``cleanup_worker_execution``. This handler covers the
    SIGKILL / SIGTERM / SIGINT / atexit paths where in-process cleanup
    cannot run — it fires from the :class:`CleanupRegistry` once.

    Best-effort: every failure is logged and swallowed because the handler
    runs during process teardown when other state may already be torn
    down. A raise here would block the registry's remaining handlers.
    """
    pid = os.getpid()
    docker = shutil.which("docker")
    if docker is None:
        logger.warning("docker executable not found; skipping PID-label cleanup")
        return
    try:
        listing = subprocess.run(  # noqa: S603 - docker path is resolved via shutil.which
            [
                docker,
                "ps",
                "-a",
                "--filter",
                f"label=arise.session_pid={pid}",
                "--format",
                "{{.ID}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        ids = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        if not ids:
            return
        subprocess.run(  # noqa: S603 - docker path is resolved via shutil.which
            [docker, "rm", "-f", *ids],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        logger.warning("docker PID-label cleanup failed", exc_info=True)
