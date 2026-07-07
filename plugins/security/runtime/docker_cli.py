"""Generic Docker subprocess plumbing for the SEC-bench runtime.

Module-level API shared by the runtime coordinator and its collaborators
(image acquisition, workspace mirroring). ``timeout`` is a required argument
because the caller owns the configured budget; there is no per-process
default here. The ``timeout`` parameters are deliberate (ASYNC109 suppressed):
these wrap ``docker`` invocations whose deadline is a task-level concern.
"""

from __future__ import annotations

import asyncio
import logging
import os


logger = logging.getLogger(__name__)


async def run_command(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:  # noqa: ASYNC109
    """Run a command, returning ``(exit_code, stdout, stderr)``.

    Raises:
        RuntimeError: if the command does not complete within ``timeout``.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as e:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("docker subprocess did not exit after kill: %s", cmd[:3])
        raise RuntimeError(f"Docker command timed out after {timeout}s: {cmd[:3]}") from e
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def run_checked(cmd: list[str], *, timeout: float) -> str:  # noqa: ASYNC109
    """Run a command and return stdout, raising ``RuntimeError`` on failure."""
    exit_code, stdout, stderr = await run_command(cmd, timeout=timeout)
    if exit_code != 0:
        raise RuntimeError(stderr.strip() or stdout.strip() or "Command failed")
    return stdout


async def run_best_effort(cmd: list[str], *, timeout: float) -> None:  # noqa: ASYNC109
    """Run a command, logging (not raising) on non-zero exit with stderr."""
    exit_code, _, stderr = await run_command(cmd, timeout=timeout)
    if exit_code != 0 and stderr.strip():
        logger.warning("Command failed during cleanup: %s", stderr.strip())
