"""Host-side mirroring of the container ``/src``, ``/testcase`` and ``/work``.

Seeds host directories from the tools image via ``docker cp`` and normalizes
their modes so the host-side agent can edit the mirrored trees. The
``timeout`` parameters are deliberate (ASYNC109 suppressed): they thread the
coordinator's configured per-command budget into the ``docker`` calls.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import docker_cli


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


async def copy_testcase_tree(image: str, testcase_dir: Path, *, timeout: float) -> None:  # noqa: ASYNC109
    seed_container = (
        await docker_cli.run_checked(
            [
                "docker",
                "create",
                "--label",
                f"arise.session_pid={os.getpid()}",
                "--label",
                "arise.role=seed",
                "--label",
                f"arise.created_at={datetime.now(UTC).isoformat()}",
                image,
            ],
            timeout=timeout,
        )
    ).strip()
    try:
        await docker_cli.run_checked(
            ["docker", "cp", f"{seed_container}:/testcase/.", str(testcase_dir)],
            timeout=timeout,
        )
    finally:
        await docker_cli.run_best_effort(["docker", "rm", "-f", seed_container], timeout=timeout)


async def copy_source_tree(image: str, source_dir: Path, *, timeout: float) -> None:  # noqa: ASYNC109
    seed_container = (
        await docker_cli.run_checked(
            [
                "docker",
                "create",
                "--label",
                f"arise.session_pid={os.getpid()}",
                "--label",
                "arise.role=seed",
                "--label",
                f"arise.created_at={datetime.now(UTC).isoformat()}",
                image,
            ],
            timeout=timeout,
        )
    ).strip()
    try:
        await docker_cli.run_checked(
            ["docker", "cp", f"{seed_container}:/src/.", str(source_dir)],
            timeout=timeout,
        )
    finally:
        await docker_cli.run_best_effort(["docker", "rm", "-f", seed_container], timeout=timeout)
    make_host_tree_writable(source_dir)
    # Ensure build.sh is executable after copy (docker cp may not preserve mode).
    build_sh = source_dir / "build.sh"
    if build_sh.exists():
        build_sh.chmod(build_sh.stat().st_mode | 0o755)


async def copy_work_tree(image: str, work_root: Path, *, timeout: float) -> None:  # noqa: ASYNC109
    seed_container = (
        await docker_cli.run_checked(
            [
                "docker",
                "create",
                "--label",
                f"arise.session_pid={os.getpid()}",
                "--label",
                "arise.role=seed",
                "--label",
                f"arise.created_at={datetime.now(UTC).isoformat()}",
                image,
            ],
            timeout=timeout,
        )
    ).strip()
    try:
        exit_code, stdout, stderr = await docker_cli.run_command(
            ["docker", "cp", f"{seed_container}:/work/.", str(work_root)],
            timeout=timeout,
        )
        if exit_code != 0:
            logger.info(
                "No /work tree copied from %s: %s",
                image,
                stderr.strip() or stdout.strip() or "docker cp failed",
            )
            return
    finally:
        await docker_cli.run_best_effort(["docker", "rm", "-f", seed_container], timeout=timeout)
    make_host_tree_writable(work_root)


def make_host_tree_writable(path: Path) -> None:
    # docker cp preserves root ownership from the image. Make mirrored
    # files writable by the host-side agent while leaving symlinks alone.
    # On hosts where the files come out root-owned (native Linux docker),
    # every chmod fails with EPERM — summarize instead of failing the run,
    # but never silently: unwritable files surface later as agent failures.
    failures = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            continue
        try:
            mode = item.stat().st_mode
            if item.is_dir():
                item.chmod(mode | 0o777)
            else:
                item.chmod(mode | 0o666)
        except OSError:
            failures += 1
    if failures:
        logger.warning(
            "Could not adjust permissions on %d mirrored entries under %s; "
            "host-side edits to those files may fail",
            failures,
            path,
        )


def map_host_work_dir(source_dir: Path, container_work_dir: str) -> Path:
    # G.5 part 1 — normalize-and-validate. `Path.__truediv__` does NOT
    # normalize, so `container_work_dir="/src/../../etc"` would produce
    # `source_dir / "../../etc"` and a subsequent `mkdir(parents=True)`
    # would create directories outside the runs root. Resolve both paths
    # and require the candidate to either equal or descend from
    # `source_dir`.
    if container_work_dir == "/src":
        return source_dir
    if not container_work_dir.startswith("/src/"):
        raise RuntimeError(f"Unsupported SEC-bench work_dir outside /src: {container_work_dir}")
    candidate = source_dir / container_work_dir.removeprefix("/src/")
    source_resolved = source_dir.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved != source_resolved and source_resolved not in candidate_resolved.parents:
        raise RuntimeError(
            f"Refusing work_dir escape: {container_work_dir} resolves to "
            f"{candidate_resolved}, outside {source_resolved}"
        )
    return candidate
