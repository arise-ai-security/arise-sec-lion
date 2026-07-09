"""Docker-backed SEC-bench runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .container_runtime import SecBenchContainerSession, SecBenchWorkspace
from .procedures import CommandOutcome
from .runtime import docker_cli, image_ensurer, sealer, workspace_mirror
from .runtime.sealer import _FORBIDDEN_TESTCASE_ARTIFACTS  # noqa: F401  re-exported for tests


if TYPE_CHECKING:
    from uuid import UUID

    from .cve_instance import CVEInstance


logger = logging.getLogger(__name__)


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
            # runs in-container (Cell A flat-mode docker-exec path).
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


class DockerProcedureSession:
    """ProcedureSession over the run's shared worker container.

    Drives ``docker exec`` synchronously for the deterministic procedure tier.
    A timeout is a task-level outcome (``CommandOutcome(timed_out=True)``),
    never an exception — the procedure turns it into a FAIL verdict digest.
    """

    def __init__(self, session: SecBenchContainerSession) -> None:
        self._session = session
        self.testcase_dir = session.workspace.host_testcase_dir

    async def run(self, command: str, *, timeout: float) -> CommandOutcome:  # noqa: ASYNC109
        argv = [
            "docker",
            "exec",
            "-i",
            "-w",
            self._session.workspace.container_working_directory,
            self._session.container_id,
            "bash",
            "-lc",
            command,
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
            except TimeoutError:
                stdout = b""
                logger.warning("docker exec did not exit after kill: %s", command[:80])
            return CommandOutcome(
                exit_code=124,
                output=stdout.decode("utf-8", errors="replace"),
                timed_out=True,
            )
        return CommandOutcome(
            exit_code=process.returncode or 0,
            output=stdout.decode("utf-8", errors="replace"),
        )
