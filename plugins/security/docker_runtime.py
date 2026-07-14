"""Docker-backed SEC-bench runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .container_runtime import SecBenchContainerSession, SecBenchWorkspace
from .procedures import CommandOutcome, CommandTermination, ProcedureInfrastructureError
from .runtime import docker_cli, image_ensurer, sealer, workspace_mirror
from .runtime.sealer import _FORBIDDEN_TESTCASE_ARTIFACTS  # noqa: F401  re-exported for tests


if TYPE_CHECKING:
    from uuid import UUID

    from .cve_instance import CVEInstance


logger = logging.getLogger(__name__)


_PROCEDURE_FIXED_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "BASH_ENV": "/dev/null",
    "ENV": "/dev/null",
}


class DockerSecBenchRuntime:
    """Prepare host workspaces and run short-lived SEC-bench containers.

    Raises:
        RuntimeError: at construction time, when running under DooD
            (``/.dockerenv`` exists or ``DOCKER_HOST`` is set) and
            ``HOST_PROJECT_ROOT`` is empty or relative. Bind mounts cannot
            resolve in that configuration and every parallel run would fail.
    """

    def __init__(
        self,
        container_prefix: str = "secbench-worker",
        *,
        network_mode: str = "host",
        timeout_seconds: float = 300.0,
        tools_image_registry: str = "",
    ) -> None:
        self._container_prefix = container_prefix
        self._network_mode = network_mode
        self._timeout_seconds = float(timeout_seconds)
        self._tools_image_registry = tools_image_registry.rstrip("/")
        # DooD path mapping: container /app → host project root.
        # When running inside a container that mounts Docker socket, volume
        # bind paths must be expressed as host paths since the Docker daemon
        # runs on the host. HOST_PROJECT_ROOT overrides automatic detection.
        self._host_project_root = os.environ.get("HOST_PROJECT_ROOT", "")
        if self._dood_mode() and not self._host_project_root_valid():
            raise RuntimeError(
                "HOST_PROJECT_ROOT must be an absolute host path when running "
                "under DooD. Set it in deployment/.env."
            )

    @staticmethod
    def _dood_mode() -> bool:
        return Path("/.dockerenv").exists() or bool(os.environ.get("DOCKER_HOST"))

    def _host_project_root_valid(self) -> bool:
        return bool(self._host_project_root) and Path(self._host_project_root).is_absolute()

    def _host_path(self, container_path: Path) -> str:
        resolved = str(container_path.resolve())
        if self._host_project_root and resolved.startswith("/app/"):
            return resolved.replace("/app/", self._host_project_root + "/", 1)
        return resolved

    async def prepare_workspace(
        self,
        cve: CVEInstance,
        run_output_path: Path,
        image: str,
        root_id: UUID,
    ) -> SecBenchWorkspace:
        """Seed host mirrors of container `/src`, `/testcase`, and `/work`."""
        run_output_path.mkdir(parents=True, exist_ok=True)
        source_dir = run_output_path / "src"
        testcase_dir = run_output_path / "testcase"
        work_root = run_output_path / "work"
        # Sealed (non-agent-writable) location, outside every bind mount — see
        # sealer.sealed_dir. The host-executed secb-exec helper lives here so a
        # root worker cannot rewrite the script the MCP server runs on the host.
        helper_script = sealer.sealed_dir(run_output_path) / "secb-exec"

        source_dir.mkdir(parents=True, exist_ok=True)
        testcase_dir.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        await image_ensurer.ensure_image_exists(
            image, registry=self._tools_image_registry, timeout=self._timeout_seconds
        )
        if not any(source_dir.iterdir()):
            await workspace_mirror.copy_source_tree(
                image, source_dir, timeout=self._timeout_seconds
            )
        if not any(testcase_dir.iterdir()):
            await workspace_mirror.copy_testcase_tree(
                image, testcase_dir, timeout=self._timeout_seconds
            )
        workspace_mirror.make_host_tree_writable(testcase_dir)
        sealer.remove_forbidden_testcase_artifacts(testcase_dir)
        sealer.seed_runtime_scripts(testcase_dir)
        if not any(work_root.iterdir()):
            await workspace_mirror.copy_work_tree(image, work_root, timeout=self._timeout_seconds)

        host_work_dir = workspace_mirror.map_host_work_dir(source_dir, cve.work_dir)
        host_work_dir.mkdir(parents=True, exist_ok=True)

        return SecBenchWorkspace(
            root_id=root_id,
            image=image,
            host_root=run_output_path,
            host_source_dir=source_dir,
            host_testcase_dir=testcase_dir,
            host_work_dir=host_work_dir,
            container_source_dir="/src",
            container_testcase_dir="/testcase",
            container_working_directory=cve.work_dir,
            helper_script=helper_script,
            sealed_surface=sealer.sealed_runtime_surface(),
        )

    async def start_session(
        self,
        cve: CVEInstance,
        workspace: SecBenchWorkspace,
        agent_id: UUID,
    ) -> SecBenchContainerSession:
        """Start a tool-enriched container bound to the prepared workspace."""
        # G.5 part 2 — assert every bind-mount target lives under this run's
        # host_root. Scope is the run's own root, not the pool root, so a
        # sibling-run path (e.g. /tmp/runs/<other-root>/src) is rejected.
        host_root_resolved = workspace.host_root.resolve()
        for label, path in (
            ("host_source_dir", workspace.host_source_dir),
            ("host_testcase_dir", workspace.host_testcase_dir),
            ("host_work_root", workspace.host_work_root),
            ("host_work_dir", workspace.host_work_dir),
        ):
            resolved = path.resolve()
            if resolved != host_root_resolved and host_root_resolved not in resolved.parents:
                raise RuntimeError(
                    f"Refusing to mount {label}={resolved}: "
                    f"path escapes this run's host_root {host_root_resolved}"
                )

        container_name = f"{self._container_prefix}-{workspace.root_id.hex}-{agent_id.hex}"
        sealer.seed_runtime_scripts(workspace.host_testcase_dir)
        patch_script = sealer.validated_patch_script(workspace.host_testcase_dir)
        secb_wrapper = sealer.sealed_secb_wrapper(workspace.host_root)
        cmd = [
            "docker",
            "run",
            "-d",
            "--network",
            self._network_mode,
            "--name",
            container_name,
            "--label",
            f"arise.root_id={workspace.root_id}",
            "--label",
            f"arise.agent_id={agent_id}",
            "--label",
            f"arise.instance_id={cve.instance_id}",
            "--label",
            f"arise.session_pid={os.getpid()}",
            "--label",
            f"arise.created_at={datetime.now(UTC).isoformat()}",
            "-v",
            f"{self._host_path(workspace.host_source_dir)}:{workspace.container_source_dir}",
            "-v",
            f"{self._host_path(workspace.host_testcase_dir)}:{workspace.container_testcase_dir}",
            "-v",
            f"{self._host_path(patch_script)}:{workspace.container_testcase_dir}/patch.sh:ro",
            "-v",
            f"{self._host_path(workspace.host_work_root)}:{workspace.container_work_dir}",
            # Bind the workspace root so worker scratch files (e.g. mcp config,
            # scratch CLAUDE_CONFIG_DIR) written on the host are reachable from
            # inside the container — required when the agent process itself
            # runs through the flat in-container docker-exec path.
            "-v",
            f"{self._host_path(workspace.host_root)}:{workspace.container_workspace_root}",
            "-v",
            f"{self._host_path(patch_script)}:{workspace.container_workspace_root}/testcase/patch.sh:ro",
            # Overlay the delegating secb wrapper read-only from a sealed source
            # outside every bind mount, so a root worker shell cannot rewrite
            # secb to fake build/repro/patch results (replaces the baked golden
            # secb at run time without leaving a writable in-container copy).
            "-v",
            f"{self._host_path(secb_wrapper)}:/usr/local/bin/secb:ro",
            workspace.image,
            "tail",
            "-f",
            "/dev/null",
        ]
        container_id = (await docker_cli.run_checked(cmd, timeout=self._timeout_seconds)).strip()[
            :12
        ]

        session = SecBenchContainerSession(
            workspace=workspace,
            container_id=container_id,
            container_name=container_name,
            image=workspace.image,
        )

        # Audit BUG-B: the container is already running after `docker run`; the
        # post-run setup below may raise (notably the exec-helper write). Without
        # a cleanup wrapper an exception leaks a detached container the caller
        # never sees (the session is never returned, so the plugin's
        # `cleanup_worker_execution` pops nothing). Force-remove before
        # re-raising so no orphan survives.
        try:
            await self._add_git_safe_directory(container_id, cve.work_dir)
            # Ensure build.sh is executable inside the container (DooD uid mismatch)
            await docker_cli.run_best_effort(
                ["docker", "exec", container_id, "chmod", "+x", "/src/build.sh"],
                timeout=self._timeout_seconds,
            )
            self._write_exec_helper(session)
        except Exception:
            await docker_cli.run_best_effort(
                ["docker", "rm", "-f", container_id], timeout=self._timeout_seconds
            )
            raise

        return session

    async def _add_git_safe_directory(self, container_id: str, work_dir: str) -> None:
        await docker_cli.run_best_effort(
            [
                "docker",
                "exec",
                container_id,
                "git",
                "config",
                "--global",
                "--add",
                "safe.directory",
                work_dir,
            ],
            timeout=self._timeout_seconds,
        )

    def _write_exec_helper(self, session: SecBenchContainerSession) -> None:
        helper = session.workspace.helper_script
        helper.parent.mkdir(parents=True, exist_ok=True)
        work_dir = session.workspace.container_working_directory
        container = session.container_id
        helper.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "",
                    'if [ "$#" -eq 0 ]; then',
                    "  echo \"Usage: ./secb-exec '<command>'\" >&2",
                    "  exit 2",
                    "fi",
                    "",
                    f"WORKDIR={shlex.quote(work_dir)}",
                    f"CONTAINER={shlex.quote(container)}",
                    "# Run with permissive umask so files created by root-in-container",
                    "# land on the host bind-mount as world-writable (mode 666/777),",
                    "# letting the host-side file editor overwrite them later.",
                    'CMD="umask 000; $*"',
                    "# Try the project workdir first; fall back to /src, then /",
                    "# so that commands survive directory deletion/re-creation.",
                    'if docker exec "$CONTAINER" test -d "$WORKDIR" 2>/dev/null; then',
                    '  docker exec -i -w "$WORKDIR" "$CONTAINER" bash -lc "$CMD"',
                    'elif docker exec "$CONTAINER" test -d /src 2>/dev/null; then',
                    '  docker exec -i -w /src "$CONTAINER" bash -lc "$CMD"',
                    "else",
                    '  docker exec -i -w / "$CONTAINER" bash -lc "$CMD"',
                    "fi",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        helper.chmod(helper.stat().st_mode | 0o755)


class _ProcedureLaunchTimedOut(TimeoutError):
    """The Host deadline expired before docker exec returned a process handle."""


class DockerProcedureSession:
    """ProcedureSession over the run's shared worker container.

    Drives ``docker exec`` synchronously under Host-owned deadlines. A timeout
    resets the shared container and becomes a task-level outcome. Startup probes
    also reset after completion or an early Host marker so no probe descendant can
    survive. Failure to reset poisons the session and is an infrastructure error.
    """

    def __init__(
        self,
        session: SecBenchContainerSession,
        *,
        on_removed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._on_removed = on_removed
        self._execution_lock = asyncio.Lock()
        self._usable = True
        self._removed = False
        self.testcase_dir = session.workspace.host_testcase_dir
        self.source_dir = session.workspace.host_source_dir
        self.work_dir = session.workspace.host_work_root
        self.identity_dir = sealer.sealed_dir(session.workspace.host_root)

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome:  # noqa: ASYNC109
        async with self._execution_lock:
            self._ensure_usable()
            return await self._run(argv, timeout=timeout, stdin=stdin, env=env)

    async def run_until_marker(
        self,
        argv: tuple[str, ...],
        *,
        marker: str,
        timeout: float,
        env: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome:  # noqa: ASYNC109
        if not marker:
            raise ValueError("procedure success marker must not be empty")
        async with self._execution_lock:
            self._ensure_usable()
            return await self._run_until_marker(
                argv,
                marker=marker,
                timeout=timeout,
                env=env,
            )

    @property
    def usable(self) -> bool:
        return self._usable

    async def _run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        stdin: bytes | None,
        env: tuple[tuple[str, str], ...],
    ) -> CommandOutcome:
        if not argv:
            raise ValueError("procedure argv must not be empty")
        if timeout <= 0:
            raise ValueError("procedure timeout must be positive")

        docker_argv = self._docker_argv(argv, env=env)
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            process = await self._start_safely(
                docker_argv,
                timeout=timeout,
                pipe_stdin=stdin is not None,
            )
        except _ProcedureLaunchTimedOut:
            return CommandOutcome(
                exit_code=124,
                output="",
                termination=CommandTermination.TIMED_OUT,
            )
        communication = asyncio.ensure_future(process.communicate(input=stdin))
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError:
            stdout = await self._finish_interruption(
                process,
                docker_argv,
                reason="timed out",
                communication=communication,
            )
            return CommandOutcome(
                exit_code=124,
                output=stdout.decode("utf-8", errors="replace"),
                termination=CommandTermination.TIMED_OUT,
            )
        except asyncio.CancelledError:
            await self._finish_interruption(
                process,
                docker_argv,
                reason="was cancelled",
                communication=communication,
            )
            raise
        except (OSError, RuntimeError):
            await self._finish_interruption(process, docker_argv, reason="failed")
            raise
        return CommandOutcome(
            exit_code=process.returncode or 0,
            output=stdout.decode("utf-8", errors="replace"),
        )

    async def _run_until_marker(
        self,
        argv: tuple[str, ...],
        *,
        marker: str,
        timeout: float,
        env: tuple[tuple[str, str], ...],
    ) -> CommandOutcome:
        if not argv:
            raise ValueError("procedure argv must not be empty")
        if timeout <= 0:
            raise ValueError("procedure timeout must be positive")

        docker_argv = self._docker_argv(argv, env=env)
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            process = await self._start_safely(
                docker_argv,
                timeout=timeout,
                pipe_stdin=False,
            )
        except _ProcedureLaunchTimedOut:
            return CommandOutcome(
                exit_code=124,
                output="",
                termination=CommandTermination.TIMED_OUT,
            )
        output = bytearray()
        try:
            marker_seen = await asyncio.wait_for(
                _read_until_marker_or_exit(
                    process,
                    marker.encode("utf-8"),
                    output,
                ),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError:
            await self._finish_interruption(process, docker_argv, reason="timed out")
            return CommandOutcome(
                exit_code=124,
                output=output.decode("utf-8", errors="replace"),
                termination=CommandTermination.TIMED_OUT,
            )
        except asyncio.CancelledError:
            await self._finish_interruption(process, docker_argv, reason="was cancelled")
            raise
        except (OSError, RuntimeError):
            await self._finish_interruption(process, docker_argv, reason="failed")
            raise

        reason = "stopped after startup marker" if marker_seen else "completed startup probe"
        output.extend(await self._finish_interruption(process, docker_argv, reason=reason))
        return CommandOutcome(
            exit_code=process.returncode or 0,
            output=output.decode("utf-8", errors="replace"),
            termination=(
                CommandTermination.STOPPED_AFTER_MARKER
                if marker_seen
                else CommandTermination.COMPLETED
            ),
        )

    def _docker_argv(
        self,
        argv: tuple[str, ...],
        *,
        env: tuple[tuple[str, str], ...],
    ) -> list[str]:
        # Docker preserves the image's OSS-Fuzz environment (SRC, WORK, OUT,
        # SANITIZER, compiler flags). Override only the command-specific values
        # plus the fixed process-launch controls, which callers cannot replace.
        procedure_env = dict(env)
        procedure_env.update(_PROCEDURE_FIXED_ENV)
        docker_env = [
            item
            for pair in procedure_env.items()
            for item in ("--env", "=".join(pair))
        ]
        return [
            "docker",
            "exec",
            "-i",
            *docker_env,
            "-w",
            self._session.workspace.container_working_directory,
            self._session.container_id,
            *argv,
        ]

    def _ensure_usable(self) -> None:
        if not self._usable:
            raise ProcedureInfrastructureError(
                "procedure container is unavailable after a failed reset"
            )

    async def _start(
        self,
        docker_argv: list[str],
        *,
        pipe_stdin: bool,
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *docker_argv,
            stdin=(asyncio.subprocess.PIPE if pipe_stdin else asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )

    async def _start_safely(
        self,
        docker_argv: list[str],
        *,
        timeout: float,
        pipe_stdin: bool,
    ) -> asyncio.subprocess.Process:
        start = asyncio.create_task(self._start(docker_argv, pipe_stdin=pipe_stdin))
        try:
            return await asyncio.wait_for(asyncio.shield(start), timeout=timeout)
        except TimeoutError as exc:
            await self._finish_launch_interruption(
                start,
                docker_argv,
                reason="timed out during launch",
            )
            raise _ProcedureLaunchTimedOut from exc
        except asyncio.CancelledError:
            await self._finish_launch_interruption(
                start,
                docker_argv,
                reason="was cancelled during launch",
            )
            raise

    async def _finish_launch_interruption(
        self,
        start: asyncio.Task[asyncio.subprocess.Process],
        docker_argv: list[str],
        *,
        reason: str,
    ) -> None:
        cleanup = asyncio.create_task(
            self._abort_launch(start, docker_argv, reason=reason)
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _abort_launch(
        self,
        start: asyncio.Task[asyncio.subprocess.Process],
        docker_argv: list[str],
        *,
        reason: str,
    ) -> None:
        self._usable = False
        start.cancel()
        removal_error = await self._force_remove_container()
        await asyncio.sleep(0)
        if start.done():
            _dispose_late_launch(start, docker_argv)
        else:
            start.add_done_callback(
                lambda completed: _dispose_late_launch(completed, docker_argv)
            )
        if removal_error is not None:
            raise ProcedureInfrastructureError(
                f"procedure {reason} and the shared container could not be removed"
            ) from removal_error
        await self._confirm_removed()

    async def _finish_interruption(
        self,
        process: asyncio.subprocess.Process,
        docker_argv: list[str],
        *,
        reason: str,
        communication: asyncio.Future[tuple[bytes, bytes]] | None = None,
    ) -> bytes:
        cleanup = asyncio.create_task(
            self._reset_and_reap(
                process,
                docker_argv,
                reason=reason,
                communication=communication,
            )
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        output = cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
        return output

    async def _reset_and_reap(
        self,
        process: asyncio.subprocess.Process,
        docker_argv: list[str],
        *,
        reason: str,
        communication: asyncio.Future[tuple[bytes, bytes]] | None,
    ) -> bytes:
        restart_error = await self._restart_container()
        removal_error: OSError | RuntimeError | None = None
        if restart_error is not None:
            self._usable = False
            removal_error = await self._force_remove_container()
        if communication is None:
            communication = asyncio.ensure_future(process.communicate())
        try:
            output = await self._reap_client(
                process,
                docker_argv,
                communication=communication,
            )
        except (OSError, RuntimeError) as reap_error:
            self._usable = False
            if restart_error is None:
                removal_error = await self._force_remove_container()
            await self._settle_failed_client(process, communication, docker_argv)
            if removal_error is None:
                await self._confirm_removed()
                raise ProcedureInfrastructureError(
                    f"procedure {reason}; the docker exec could not be reaped, "
                    "so the shared container was removed"
                ) from reap_error
            raise ProcedureInfrastructureError(
                f"procedure {reason}; the docker exec could not be reaped and "
                "the shared container could not be removed"
            ) from removal_error
        if restart_error is not None:
            if removal_error is None:
                await self._confirm_removed()
            detail = "restarted or removed" if removal_error is not None else "restarted"
            raise ProcedureInfrastructureError(
                f"procedure {reason} and the shared container could not be {detail}"
            ) from (removal_error or restart_error)
        return output

    async def _restart_container(self) -> OSError | RuntimeError | None:
        try:
            await docker_cli.run_checked(
                ["docker", "restart", "--timeout", "0", self._session.container_id],
                timeout=60.0,
            )
        except (OSError, RuntimeError) as exc:
            return exc
        return None

    async def _force_remove_container(self) -> OSError | RuntimeError | None:
        try:
            await docker_cli.run_checked(
                ["docker", "rm", "-f", self._session.container_id],
                timeout=60.0,
            )
        except (OSError, RuntimeError) as exc:
            logger.warning("procedure container could not be force-removed: %s", exc)
            return exc
        return None

    async def _confirm_removed(self) -> None:
        self._usable = False
        if self._removed:
            return
        if self._on_removed is not None:
            await self._on_removed()
        self._removed = True

    async def _reap_client(
        self,
        process: asyncio.subprocess.Process,
        docker_argv: list[str],
        *,
        communication: asyncio.Future[tuple[bytes, bytes]],
    ) -> bytes:
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=5.0,
            )
            return stdout
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=5.0,
            )
            return stdout
        except TimeoutError as exc:
            logger.warning(
                "docker exec did not exit after container reset: %s",
                docker_argv[-8:],
            )
            raise ProcedureInfrastructureError(
                "interrupted docker exec could not be reaped after container reset"
            ) from exc

    async def _settle_failed_client(
        self,
        process: asyncio.subprocess.Process,
        communication: asyncio.Future[tuple[bytes, bytes]],
        docker_argv: list[str],
    ) -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        communication.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(communication), timeout=5.0)
        except (asyncio.CancelledError, TimeoutError, OSError, RuntimeError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (TimeoutError, OSError, RuntimeError):
            logger.warning(
                "docker exec process did not terminate after container removal: %s",
                docker_argv[-8:],
            )


def _dispose_late_launch(
    start: asyncio.Task[asyncio.subprocess.Process],
    docker_argv: list[str],
) -> None:
    try:
        process = start.result()
    except (asyncio.CancelledError, OSError, RuntimeError):
        return
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            return
        logger.warning(
            "killed docker exec client returned after container removal: %s",
            docker_argv[-8:],
        )


async def _read_until_marker_or_exit(
    process: asyncio.subprocess.Process,
    marker: bytes,
    output: bytearray,
) -> bool:
    if process.stdout is None:
        raise ProcedureInfrastructureError("marker-aware docker exec has no output stream")
    while chunk := await process.stdout.read(4096):
        search_from = max(0, len(output) - len(marker) + 1)
        output.extend(chunk)
        if output.find(marker, search_from) >= 0:
            return True
    await process.wait()
    return False
