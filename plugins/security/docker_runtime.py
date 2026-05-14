"""Docker-backed SEC-bench runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from .container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
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
    ) -> None:
        self._container_prefix = container_prefix
        self._network_mode = network_mode
        self._timeout_seconds = float(timeout_seconds)
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
        helper_script = run_output_path / "secb-exec"

        source_dir.mkdir(parents=True, exist_ok=True)
        testcase_dir.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        await self._ensure_image_exists(image)
        if not any(source_dir.iterdir()):
            await self._copy_source_tree(image, source_dir)
        if not any(testcase_dir.iterdir()):
            await self._copy_testcase_tree(image, testcase_dir)
        if not any(work_root.iterdir()):
            await self._copy_work_tree(image, work_root)

        host_work_dir = self._map_host_work_dir(source_dir, cve.work_dir)
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
            if (
                resolved != host_root_resolved
                and host_root_resolved not in resolved.parents
            ):
                raise RuntimeError(
                    f"Refusing to mount {label}={resolved}: "
                    f"path escapes this run's host_root {host_root_resolved}"
                )

        container_name = (
            f"{self._container_prefix}-{workspace.root_id.hex}-{agent_id.hex}"
        )
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
            f"{self._host_path(workspace.host_work_root)}:{workspace.container_work_dir}",
            # Bind the workspace root so worker scratch files (e.g. mcp config,
            # scratch CLAUDE_CONFIG_DIR) written on the host are reachable from
            # inside the container — required when the agent process itself
            # runs in-container (Cell A flat-mode docker-exec path).
            "-v",
            f"{self._host_path(workspace.host_root)}:{workspace.container_workspace_root}",
            workspace.image,
            "tail",
            "-f",
            "/dev/null",
        ]
        container_id = (await self._run_checked(cmd)).strip()[:12]

        # Audit BUG-B: the container is already running after `docker run`;
        # the post-run setup (`_install_secb`, etc.) may raise. Without a
        # cleanup wrapper an exception leaks a detached container the
        # caller never sees (the session object is never returned, so the
        # plugin's `cleanup_worker_execution` pops nothing). Force-remove
        # the container before re-raising so no orphan survives.
        try:
            if cve.secb_sh:
                await self._install_secb(container_id, cve.secb_sh)

            await self._add_git_safe_directory(container_id, cve.work_dir)
            # Ensure build.sh is executable inside the container (DooD uid mismatch)
            await self._run_best_effort(
                ["docker", "exec", container_id, "chmod", "+x", "/src/build.sh"]
            )
        except Exception:
            await self._run_best_effort(["docker", "rm", "-f", container_id])
            raise

        session = SecBenchContainerSession(
            workspace=workspace,
            container_id=container_id,
            container_name=container_name,
            image=workspace.image,
        )
        self._write_exec_helper(session)
        return session

    async def stop_session(self, session: SecBenchContainerSession) -> None:
        """Stop and remove a worker container."""
        await self._run_best_effort(["docker", "rm", "-f", session.container_id])

    async def _ensure_image_exists(self, image: str) -> None:
        exit_code, _, _ = await self._run_command(["docker", "image", "inspect", image])
        if exit_code == 0:
            return
        raise RuntimeError(
            "Missing SEC-bench image "
            f"{image}. Build it first with deployment/build-secbench-tools.sh."
        )

    async def _copy_testcase_tree(self, image: str, testcase_dir: Path) -> None:
        seed_container = (
            await self._run_checked(
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
                ]
            )
        ).strip()
        try:
            await self._run_checked(
                ["docker", "cp", f"{seed_container}:/testcase/.", str(testcase_dir)]
            )
        finally:
            await self._run_best_effort(["docker", "rm", "-f", seed_container])

    async def _add_git_safe_directory(self, container_id: str, work_dir: str) -> None:
        await self._run_best_effort(
            [
                "docker", "exec", container_id, "git", "config",
                "--global", "--add", "safe.directory", work_dir,
            ]
        )

    async def _copy_source_tree(self, image: str, source_dir: Path) -> None:
        seed_container = (
            await self._run_checked(
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
                ]
            )
        ).strip()
        try:
            await self._run_checked(
                ["docker", "cp", f"{seed_container}:/src/.", str(source_dir)]
            )
        finally:
            await self._run_best_effort(["docker", "rm", "-f", seed_container])
        self._make_host_tree_writable(source_dir)
        # Ensure build.sh is executable after copy (docker cp may not preserve mode).
        build_sh = source_dir / "build.sh"
        if build_sh.exists():
            build_sh.chmod(build_sh.stat().st_mode | 0o755)

    async def _copy_work_tree(self, image: str, work_root: Path) -> None:
        seed_container = (
            await self._run_checked(
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
                ]
            )
        ).strip()
        try:
            exit_code, stdout, stderr = await self._run_command(
                ["docker", "cp", f"{seed_container}:/work/.", str(work_root)]
            )
            if exit_code != 0:
                logger.info(
                    "No /work tree copied from %s: %s",
                    image,
                    stderr.strip() or stdout.strip() or "docker cp failed",
                )
                return
        finally:
            await self._run_best_effort(["docker", "rm", "-f", seed_container])
        self._make_host_tree_writable(work_root)

    @staticmethod
    def _make_host_tree_writable(path: Path) -> None:
        # docker cp preserves root ownership from the image. Make mirrored
        # files writable by the host-side agent while leaving symlinks alone.
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
                pass

    def _map_host_work_dir(self, source_dir: Path, container_work_dir: str) -> Path:
        # G.5 part 1 — normalize-and-validate. `Path.__truediv__` does NOT
        # normalize, so `container_work_dir="/src/../../etc"` would produce
        # `source_dir / "../../etc"` and a subsequent `mkdir(parents=True)`
        # would create directories outside the runs root. Resolve both paths
        # and require the candidate to either equal or descend from
        # `source_dir`.
        if container_work_dir == "/src":
            return source_dir
        if not container_work_dir.startswith("/src/"):
            raise RuntimeError(
                f"Unsupported SEC-bench work_dir outside /src: {container_work_dir}"
            )
        candidate = source_dir / container_work_dir.removeprefix("/src/")
        source_resolved = source_dir.resolve()
        candidate_resolved = candidate.resolve()
        if (
            candidate_resolved != source_resolved
            and source_resolved not in candidate_resolved.parents
        ):
            raise RuntimeError(
                f"Refusing work_dir escape: {container_work_dir} resolves to "
                f"{candidate_resolved}, outside {source_resolved}"
            )
        return candidate

    async def _install_secb(self, container_id: str, secb_content: str) -> None:
        install_cmd = f"""
cat > /usr/local/bin/secb << 'SECB_EOF'
{secb_content}
SECB_EOF
chmod +x /usr/local/bin/secb
"""
        await self._run_checked(
            ["docker", "exec", container_id, "bash", "-lc", install_cmd]
        )

    def _write_exec_helper(self, session: SecBenchContainerSession) -> None:
        helper = session.workspace.helper_script
        work_dir = session.workspace.container_working_directory
        container = session.container_id
        helper.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "",
                    'if [ "$#" -eq 0 ]; then',
                    '  echo "Usage: ./secb-exec \'<command>\'" >&2',
                    "  exit 2",
                    "fi",
                    "",
                    f"WORKDIR={shlex.quote(work_dir)}",
                    f"CONTAINER={shlex.quote(container)}",
                    '# Run with permissive umask so files created by root-in-container',
                    '# land on the host bind-mount as world-writable (mode 666/777),',
                    '# letting the host-side file editor overwrite them later.',
                    'CMD="umask 000; $*"',
                    '# Try the project workdir first; fall back to /src, then /',
                    '# so that commands survive directory deletion/re-creation.',
                    'if docker exec "$CONTAINER" test -d "$WORKDIR" 2>/dev/null; then',
                    '  docker exec -i -w "$WORKDIR" "$CONTAINER" bash -lc "$CMD"',
                    'elif docker exec "$CONTAINER" test -d /src 2>/dev/null; then',
                    '  docker exec -i -w /src "$CONTAINER" bash -lc "$CMD"',
                    'else',
                    '  docker exec -i -w / "$CONTAINER" bash -lc "$CMD"',
                    "fi",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        helper.chmod(helper.stat().st_mode | 0o755)

    async def _run_checked(self, cmd: list[str]) -> str:
        exit_code, stdout, stderr = await self._run_command(cmd)
        if exit_code != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or "Command failed")
        return stdout

    async def _run_best_effort(self, cmd: list[str]) -> None:
        exit_code, _, stderr = await self._run_command(cmd)
        if exit_code != 0 and stderr.strip():
            logger.warning("Command failed during cleanup: %s", stderr.strip())

    async def _run_command(self, cmd: list[str]) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "docker subprocess did not exit after kill: %s", cmd[:3]
                )
            raise RuntimeError(
                f"Docker command timed out after {self._timeout_seconds}s: {cmd[:3]}"
            )
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
