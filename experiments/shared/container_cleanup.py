"""PID-labeled Docker cleanup collaborator shared by the harness and the matrix.

Two callers reuse these primitives for different operations, both keyed on the
``arise.session_pid`` container label (:data:`SESSION_PID_LABEL`):

- the harness (via the subprocess runner) kills a hung ``main.py run`` process
  tree and then removes the containers labeled with *that* pid
  (:func:`_terminate_process_tree` + :func:`_cleanup_labeled_containers_for_pid`,
  async);
- the matrix startup sweep removes containers whose owning session pid is *stale*
  (no longer alive) before a new run begins (:func:`_sweep_stale_containers`,
  synchronous).

These are deliberately kept as distinct operations; only the shared label and the
docker invocation pattern are common.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from contextlib import suppress

from infrastructure.cleanup.registry import pid_alive


logger = logging.getLogger(__name__)

# Container label set by ``plugins/security/docker_runtime.py`` carrying the
# owning ``main.py run`` session pid. Both cleanup paths filter on it.
SESSION_PID_LABEL = "arise.session_pid"


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    reason: str,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return

    logger.warning(
        "terminating process group for pid=%d after %s; grace=%.1fs",
        process.pid,
        reason,
        grace_seconds,
    )
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        logger.warning(
            "process pid=%d ignored SIGTERM after %.1fs; sending SIGKILL",
            process.pid,
            grace_seconds,
        )
    finally:
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()

    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)


async def _cleanup_labeled_containers_for_pid(pid: int) -> None:
    container_ids = await _docker_container_ids_for_pid(pid)
    if not container_ids:
        return

    logger.warning(
        "removing %d Docker container(s) labeled arise.session_pid=%d",
        len(container_ids),
        pid,
    )
    await _run_docker_cleanup("docker", "rm", "-f", *container_ids, deadline_seconds=60.0)


async def _docker_container_ids_for_pid(pid: int) -> list[str]:
    result = await _run_docker_cleanup(
        "docker",
        "ps",
        "-a",
        "--filter",
        f"label={SESSION_PID_LABEL}={pid}",
        "--format",
        "{{.ID}}",
        deadline_seconds=30.0,
    )
    if result is None:
        return []
    return [line.strip() for line in result.splitlines() if line.strip()]


async def _run_docker_cleanup(*cmd: str, deadline_seconds: float) -> str | None:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=deadline_seconds,
        )
    except FileNotFoundError:
        logger.warning("docker executable not found; skipping container cleanup")
        return None
    except TimeoutError:
        logger.warning("docker cleanup command timed out: %s", " ".join(cmd))
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return None

    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        logger.warning(
            "docker cleanup command failed with %d: %s%s",
            process.returncode,
            " ".join(cmd),
            f" ({stderr_text})" if stderr_text else "",
        )
        return None
    return stdout.decode("utf-8", errors="replace")


def _sweep_stale_containers() -> None:
    """Remove worker/seed containers whose owning session PID is gone.

    Filters on the ``arise.session_pid`` label set by
    ``plugins/security/docker_runtime.py``. PID-reuse is an accepted
    trade-off: an unlikely PID collision means we leave the container
    alone, which is the safe failure mode.
    """
    docker = shutil.which("docker")
    if docker is None:
        logger.warning("startup sweep: docker executable not found; skipping")
        return

    try:
        result = subprocess.run(  # noqa: S603 - docker path is resolved via shutil.which
            [
                docker,
                "ps",
                "-a",
                "--filter",
                f"label={SESSION_PID_LABEL}",
                "--format",
                '{{.ID}}\t{{.Label "' + SESSION_PID_LABEL + '"}}',
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        logger.warning("startup sweep: docker ps failed (%s); skipping", exc)
        return

    stale_ids: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        cid, _, pid_str = line.partition("\t")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid_alive(pid):
            continue
        stale_ids.append(cid)

    if not stale_ids:
        return
    logger.info("startup sweep: removing %d stale worker container(s)", len(stale_ids))
    subprocess.run(  # noqa: S603 - docker path is resolved via shutil.which
        [docker, "rm", "-f", *stale_ids],
        check=False,
        capture_output=True,
        timeout=60,
    )
