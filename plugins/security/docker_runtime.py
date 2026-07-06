"""Docker-backed SEC-bench runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import shlex
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from core.ports.domain_plugin_port import SealedRuntimeArtifact, SealedRuntimeSurface

from .container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
from .cve_instance import CVEInstance
from .procedures import CommandOutcome


logger = logging.getLogger(__name__)

# Image pulls move multi-GB layers; give them far more headroom than the
# per-command default, which is sized for short docker exec/inspect calls.
_IMAGE_PULL_TIMEOUT_SECONDS = 1800.0
_FORBIDDEN_TESTCASE_ARTIFACTS = frozenset(
    {
        "model_patch.diff",
        "gold.patch",
        "gold_patch.diff",
        "gold_patch.patch",
        "candidate_fix.diff",
        "candidate_fix.patch",
        "candidate_fixes.diff",
        "candidate_fixes.patch",
    }
)

_SECB_WRAPPER = """#!/bin/bash
set -euo pipefail

command="${1:-}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$command" in
  build)
    # Strip REPLAY_ENABLED so compile always runs $SRC/build.sh (the agent-editable
    # recipe), never a baked golden $SRC/replay_build.sh.
    exec env -u REPLAY_ENABLED /usr/local/bin/compile "$@"
    ;;
  repro)
    exec /testcase/repro.sh "$@"
    ;;
  patch)
    exec /testcase/patch.sh "$@"
    ;;
  *)
    echo "usage: secb {build|repro|patch} [args...]" >&2
    exit 2
    ;;
esac
"""

_REPRO_SKELETON = """#!/bin/bash
set -euo pipefail
echo "Arise seeded an empty /testcase/repro.sh; Exploiter must replace it." >&2
exit 2
"""

_PATCH_SCRIPT = """#!/bin/bash
set -euo pipefail

apply_if_needed() {
  local patch_file="$1"
  if [ ! -s "$patch_file" ]; then
    return 0
  fi
  if git apply --check "$patch_file"; then
    git apply "$patch_file"
    return 0
  fi
  if git apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    echo "$patch_file already applied" >&2
    return 0
  fi
  git apply --check "$patch_file"
}

apply_if_needed /testcase/repo_changes.diff

if [ ! -s /testcase/model_patch.diff ]; then
  echo "/testcase/model_patch.diff is required and must be non-empty" >&2
  exit 1
fi

apply_if_needed /testcase/model_patch.diff
"""


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
        # _sealed_dir. The host-executed secb-exec helper lives here so a root
        # worker cannot rewrite the script the MCP server runs on the host.
        helper_script = self._sealed_dir(run_output_path) / "secb-exec"

        source_dir.mkdir(parents=True, exist_ok=True)
        testcase_dir.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        await self._ensure_image_exists(image)
        if not any(source_dir.iterdir()):
            await self._copy_source_tree(image, source_dir)
        if not any(testcase_dir.iterdir()):
            await self._copy_testcase_tree(image, testcase_dir)
        self._make_host_tree_writable(testcase_dir)
        self._remove_forbidden_testcase_artifacts(testcase_dir)
        self._seed_runtime_scripts(testcase_dir)
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
            sealed_surface=self._sealed_runtime_surface(),
        )

    @staticmethod
    def _sealed_runtime_surface() -> SealedRuntimeSurface:
        """Describe the agent-visible runtime surface Arise overwrites (anti-leak).

        The secb wrapper is installed per worker container in ``start_session``,
        but its content/path are fixed constants, so it is described here at
        prep time alongside the repro/patch scripts seeded into ``/testcase``.
        """
        def _sha(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        return SealedRuntimeSurface(
            surface="secbench",
            artifacts=(
                SealedRuntimeArtifact(
                    container_path="/testcase/repro.sh",
                    kind="repro_skeleton",
                    content_sha256=_sha(_REPRO_SKELETON),
                ),
                SealedRuntimeArtifact(
                    container_path="/testcase/patch.sh",
                    kind="patch_script",
                    content_sha256=_sha(_PATCH_SCRIPT),
                ),
                SealedRuntimeArtifact(
                    container_path="/usr/local/bin/secb",
                    kind="secb_wrapper",
                    content_sha256=_sha(_SECB_WRAPPER),
                ),
            ),
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
        self._seed_runtime_scripts(workspace.host_testcase_dir)
        patch_script = self._validated_patch_script(workspace.host_testcase_dir)
        secb_wrapper = self._sealed_secb_wrapper(workspace.host_root)
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
        container_id = (await self._run_checked(cmd)).strip()[:12]

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
            await self._run_best_effort(
                ["docker", "exec", container_id, "chmod", "+x", "/src/build.sh"]
            )
            self._write_exec_helper(session)
        except Exception:
            await self._run_best_effort(["docker", "rm", "-f", container_id])
            raise

        return session

    async def stop_session(self, session: SecBenchContainerSession) -> None:
        """Stop and remove a worker container."""
        await self._run_best_effort(["docker", "rm", "-f", session.container_id])

    async def _ensure_image_exists(self, image: str) -> None:
        exit_code, _, _ = await self._run_command(["docker", "image", "inspect", image])
        if exit_code == 0:
            return
        if self._tools_image_registry and await self._pull_from_registry(image):
            return
        raise RuntimeError(
            "Missing SEC-bench image "
            f"{image}. Build it first with deployment/build-secbench-tools.sh."
        )

    async def _pull_from_registry(self, image: str) -> bool:
        """Pull a prebuilt tools image from the configured registry and alias it
        to the local tag, so a fresh machine runs without building.

        Best-effort: a pull or retag failure returns False, letting the caller
        fall back to the build-it-yourself error.
        """
        remote = f"{self._tools_image_registry}/{image}"
        logger.info("Image %s not found locally; pulling %s", image, remote)
        pull_code, _, pull_err = await self._run_command(
            ["docker", "pull", remote], timeout=_IMAGE_PULL_TIMEOUT_SECONDS
        )
        if pull_code != 0:
            logger.warning("Pull of %s failed: %s", remote, pull_err.strip())
            return False
        tag_code, _, tag_err = await self._run_command(["docker", "tag", remote, image])
        if tag_code != 0:
            logger.warning("Retag %s -> %s failed: %s", remote, image, tag_err.strip())
            return False
        return True

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

    def _seed_runtime_scripts(self, testcase_dir: Path) -> None:
        """Overwrite agent-visible runtime scripts with Arise-owned non-golden versions."""
        self._replace_regular_file(testcase_dir / "repro.sh", _REPRO_SKELETON, 0o755)
        self._replace_regular_file(testcase_dir / "patch.sh", _PATCH_SCRIPT, 0o555)

    @staticmethod
    def _replace_regular_file(path: Path, content: str, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()

        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            tmp_path.chmod(mode)
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validated_patch_script(testcase_dir: Path) -> Path:
        patch_script = testcase_dir / "patch.sh"
        if patch_script.is_symlink() or not patch_script.is_file():
            raise RuntimeError(f"Refusing to mount non-regular patch helper: {patch_script}")
        testcase_root = testcase_dir.resolve()
        patch_resolved = patch_script.resolve()
        if testcase_root not in patch_resolved.parents:
            raise RuntimeError(
                f"Refusing to mount patch helper outside testcase dir: {patch_resolved}"
            )
        return patch_script

    @staticmethod
    def _sealed_dir(host_root: Path) -> Path:
        """Per-run directory for sealed runtime files the worker must not tamper.

        A sibling of the run workspace, so it sits outside every container bind
        mount (``/src``, ``/testcase``, ``/work``, ``/arise-run``) and has no
        worker path alias. A root worker shell therefore has no filesystem route
        to the host-executed ``secb-exec`` helper or the ``secb`` wrapper source
        — unlike anything under the run workspace, which is all agent-reachable.
        """
        return host_root.parent / f"{host_root.name}.sealed"

    def _sealed_secb_wrapper(self, host_root: Path) -> Path:
        """Write the delegating ``secb`` wrapper to the sealed dir; return its path.

        Bound read-only over ``/usr/local/bin/secb`` so a root worker cannot
        rewrite ``secb`` to fake build/repro/patch. The source lives outside
        every bind mount, so there is no writable in-container alias to it.
        """
        secb = self._sealed_dir(host_root) / "secb"
        self._replace_regular_file(secb, _SECB_WRAPPER, 0o555)
        return secb

    @staticmethod
    def _remove_forbidden_testcase_artifacts(testcase_dir: Path) -> None:
        for artifact_name in _FORBIDDEN_TESTCASE_ARTIFACTS:
            artifact = testcase_dir / artifact_name
            if not artifact.exists() and not artifact.is_symlink():
                continue
            if artifact.is_dir() and not artifact.is_symlink():
                shutil.rmtree(artifact)
            else:
                artifact.unlink()

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

    async def _run_command(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> tuple[int, str, str]:
        effective_timeout = timeout if timeout is not None else self._timeout_seconds
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=effective_timeout
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
                f"Docker command timed out after {effective_timeout}s: {cmd[:3]}"
            )
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


class DockerProcedureSession:
    """ProcedureSession over the run's shared worker container.

    Drives ``docker exec`` synchronously for the deterministic procedure tier.
    A timeout is a task-level outcome (``CommandOutcome(timed_out=True)``),
    never an exception — the procedure turns it into a FAIL verdict digest.
    """

    def __init__(self, session: SecBenchContainerSession) -> None:
        self._session = session
        self.testcase_dir = session.workspace.host_testcase_dir

    async def run(self, command: str, *, timeout: float) -> CommandOutcome:
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
        except asyncio.TimeoutError:
            process.kill()
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
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
