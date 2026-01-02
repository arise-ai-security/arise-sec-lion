"""SEC-bench container lifecycle management.

Manages Sec-Bench Docker containers for vulnerability verification:
- Start container with testcase volume mount
- Install secb helper script from CVEInstance
- Keep container running for manual verification
- Stop container when done

Uses Docker-out-of-Docker (DooD) - Docker CLI inside Arise container
communicates with host Docker via mounted /var/run/docker.sock.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.domain.values.cve_instance import CVEInstance

logger = logging.getLogger(__name__)


class SecBenchContainerManager:
    """Manages Sec-Bench Docker container lifecycle.

    Provides async methods for:
    - Starting containers with volume mounts
    - Installing secb helper scripts
    - Stopping containers

    Container stays running after worker execution for manual verification.
    """

    CONTAINER_PREFIX = "secb-worker"

    async def start_container(
        self,
        cve: CVEInstance,
        testcase_path: Path,
    ) -> str:
        """Start Sec-Bench container with testcase volume mount.

        Args:
            cve: CVE instance containing docker_image and secb_sh.
            testcase_path: Local path to mount as /testcase in container.

        Returns:
            Container ID (short form, first 12 chars).

        Raises:
            RuntimeError: If container fails to start.
        """
        container_name = f"{self.CONTAINER_PREFIX}-{uuid4().hex[:8]}"
        testcase_abs = testcase_path.resolve()

        # Ensure testcase directory exists
        testcase_abs.mkdir(parents=True, exist_ok=True)

        # For DooD (Docker-out-of-Docker):
        # /app/output inside Arise container = deployment_secbench_output volume
        # Mount the same volume in SEC-bench container at /arise_output
        # Then symlink /testcase to the specific UUID subdirectory
        subdir = testcase_abs.name  # Just the UUID directory name

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            # SEC-bench images are amd64, use platform flag for Apple Silicon
            "--platform",
            "linux/amd64",
            # Mount the shared volume (same one docker-compose uses)
            "-v",
            "deployment_secbench_output:/arise_output",
            # Use bash to create symlink and sleep
            "--entrypoint",
            "/bin/bash",
            cve.docker_image,
            "-c",
            f"ln -sf /arise_output/{subdir} /testcase && sleep infinity",
        ]

        logger.info(
            f"Starting Sec-Bench container: {cve.docker_image}",
            extra={
                "container_name": container_name,
                "testcase_mount": str(testcase_abs),
                "instance_id": cve.instance_id,
            },
        )

        result = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await result.communicate()

        if result.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"Failed to start container: {error_msg}")
            raise RuntimeError(f"Docker run failed: {error_msg}")

        container_id = stdout.decode().strip()[:12]
        logger.info(f"Container started: {container_id}")

        # Install secb helper script
        if cve.secb_sh:
            await self._install_secb(container_id, cve.secb_sh)

        return container_id

    async def _install_secb(self, container_id: str, secb_content: str) -> None:
        """Install secb helper script inside container.

        Creates /usr/local/bin/secb with the content from CVEInstance.secb_sh.

        Args:
            container_id: Running container ID.
            secb_content: Content of the secb shell script.
        """
        # Use heredoc to handle special characters in script content
        install_cmd = f"""cat > /usr/local/bin/secb << 'SECB_SCRIPT_EOF'
{secb_content}
SECB_SCRIPT_EOF
chmod +x /usr/local/bin/secb"""

        cmd = [
            "docker",
            "exec",
            container_id,
            "/bin/bash",
            "-c",
            install_cmd,
        ]

        logger.debug(f"Installing secb in container {container_id}")

        result = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await result.communicate()

        if result.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.warning(f"Failed to install secb: {error_msg}")
        else:
            logger.info(f"secb installed in container {container_id}")

    async def stop_container(self, container_id: str) -> None:
        """Stop and remove container.

        Args:
            container_id: Container ID to stop.
        """
        logger.info(f"Stopping container: {container_id}")

        # Stop container
        stop_cmd = ["docker", "stop", container_id]
        await asyncio.create_subprocess_exec(
            *stop_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Remove container
        rm_cmd = ["docker", "rm", container_id]
        await asyncio.create_subprocess_exec(
            *rm_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        logger.info(f"Container removed: {container_id}")

    async def exec_command(
        self,
        container_id: str,
        command: str,
        workdir: str | None = None,
    ) -> dict[str, Any]:
        """Execute command inside container.

        Utility method for running commands in the container.

        Args:
            container_id: Target container ID.
            command: Shell command to execute.
            workdir: Working directory inside container (optional).

        Returns:
            Dict with 'returncode', 'stdout', 'stderr'.
        """
        cmd = ["docker", "exec"]
        if workdir:
            cmd.extend(["-w", workdir])
        cmd.extend([container_id, "/bin/bash", "-c", command])

        result = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await result.communicate()

        return {
            "returncode": result.returncode,
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
        }

    async def is_running(self, container_id: str) -> bool:
        """Check if container is still running.

        Args:
            container_id: Container ID to check.

        Returns:
            True if container is running, False otherwise.
        """
        cmd = [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            container_id,
        ]

        result = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await result.communicate()

        return stdout.decode().strip().lower() == "true"
