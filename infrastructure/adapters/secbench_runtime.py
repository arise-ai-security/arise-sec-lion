"""Docker-backed SEC-bench runtime."""

import asyncio
import logging
import os
import shlex
from pathlib import Path
from uuid import UUID

from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)
from plugins.security.cve_instance import CVEInstance


logger = logging.getLogger(__name__)


class DockerSecBenchRuntime:
    """Prepare host workspaces and run short-lived SEC-bench containers."""

    def __init__(self, container_prefix: str = "secbench-worker") -> None:
        self._container_prefix = container_prefix

    async def prepare_workspace(
        self,
        cve: CVEInstance,
        run_output_path: Path,
        image: str,
        root_id: UUID,
    ) -> SecBenchWorkspace:
        """Seed a host mirror of container `/src` and `/testcase`."""
        run_output_path.mkdir(parents=True, exist_ok=True)
        source_dir = run_output_path / "src"
        testcase_dir = run_output_path / "testcase"
        helper_script = run_output_path / "secb-exec"

        source_dir.mkdir(parents=True, exist_ok=True)
        testcase_dir.mkdir(parents=True, exist_ok=True)

        await self._ensure_image_exists(image)
        if not any(source_dir.iterdir()):
            await self._copy_source_tree(image, source_dir)

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
        container_name = (
            f"{self._container_prefix}-{workspace.root_id.hex[:8]}-{agent_id.hex[:8]}"
        )
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--label",
            f"arise.root_id={workspace.root_id}",
            "--label",
            f"arise.agent_id={agent_id}",
            "--label",
            f"arise.instance_id={cve.instance_id}",
            "-v",
            f"{workspace.host_source_dir.resolve()}:{workspace.container_source_dir}",
            "-v",
            f"{workspace.host_testcase_dir.resolve()}:{workspace.container_testcase_dir}",
            workspace.image,
            "tail",
            "-f",
            "/dev/null",
        ]
        container_id = (await self._run_checked(cmd)).strip()[:12]

        if cve.secb_sh:
            await self._install_secb(container_id, cve.secb_sh)

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

    async def _copy_source_tree(self, image: str, source_dir: Path) -> None:
        seed_container = (await self._run_checked(["docker", "create", image])).strip()
        try:
            await self._run_checked(
                ["docker", "cp", f"{seed_container}:/src/.", str(source_dir)]
            )
        finally:
            await self._run_best_effort(["docker", "rm", "-f", seed_container])

    def _map_host_work_dir(self, source_dir: Path, container_work_dir: str) -> Path:
        if container_work_dir == "/src":
            return source_dir
        if container_work_dir.startswith("/src/"):
            return source_dir / container_work_dir.removeprefix("/src/")
        raise RuntimeError(
            f"Unsupported SEC-bench work_dir outside /src: {container_work_dir}"
        )

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
                    "docker exec -i "
                    f"-w {shlex.quote(session.workspace.container_working_directory)} "
                    f"{shlex.quote(session.container_id)} "
                    'bash -lc "$*"',
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
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
