"""Docker adapter for SEC-bench container lifecycle management.

Implements Docker-out-of-Docker (DooD) pattern where the Arise container
communicates with the host Docker daemon via mounted /var/run/docker.sock.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from core.ports.secbench_container_port import (
    ContainerInfo,
    SecBenchContainerPort,
    SecbResult,
)

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance


class DockerContainerAdapter(SecBenchContainerPort):
    """Docker adapter implementing container lifecycle management.

    Uses Docker CLI commands via asyncio subprocess for container operations.
    Follows DooD pattern for container management from within Docker.
    """

    def __init__(
        self,
        docker_socket: str = "/var/run/docker.sock",
        network: str = "host",
    ):
        """Initialize Docker adapter.

        Args:
            docker_socket: Path to Docker socket for DooD.
            network: Docker network mode (default: host).
        """
        self._socket = docker_socket
        self._network = network
        self._containers: dict[str, ContainerInfo] = {}

    async def start_container(
        self,
        cve: "CVEInstance",
        testcase_path: Path,
        root_id: UUID,
    ) -> ContainerInfo:
        """Start SEC-bench container with testcase volume mount.

        Args:
            cve: CVE instance containing docker_image and secb_sh.
            testcase_path: Local path to mount as /testcase in container.
            root_id: Root agent ID for tracking/labeling.

        Returns:
            ContainerInfo with container_id and status.

        Raises:
            RuntimeError: If container fails to start.
        """
        image = cve.docker_image
        work_dir = cve.work_dir

        # Ensure testcase directory exists
        testcase_path.mkdir(parents=True, exist_ok=True)

        # Build docker run command
        container_name = f"secbench-{cve.instance_id}-{str(root_id)[:8]}"

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--network",
            self._network,
            "-v",
            f"{testcase_path.absolute()}:/testcase",
            "-v",
            f"{self._socket}:/var/run/docker.sock",
            "--label",
            f"arise.root_id={root_id}",
            "--label",
            f"arise.instance_id={cve.instance_id}",
            image,
            "tail",
            "-f",
            "/dev/null",  # Keep container running
        ]

        # Start container
        exit_code, stdout, stderr = await self._run_command(cmd)

        if exit_code != 0:
            raise RuntimeError(f"Failed to start container: {stderr}")

        container_id = stdout.strip()[:12]

        # Install secb helper script if provided
        if cve.secb_sh:
            await self._install_secb_script(container_id, cve.secb_sh)

        info = ContainerInfo(
            container_id=container_id,
            image=image,
            status="running",
            work_dir=work_dir,
        )

        self._containers[container_id] = info
        return info

    async def stop_container(self, container_id: str) -> None:
        """Stop and remove container.

        Args:
            container_id: Container ID to stop.
        """
        # Stop container
        await self._run_command(["docker", "stop", container_id])

        # Remove container
        await self._run_command(["docker", "rm", "-f", container_id])

        # Update tracking
        if container_id in self._containers:
            del self._containers[container_id]

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

        exit_code, stdout, _ = await self._run_command(cmd)

        if exit_code != 0:
            return False

        return stdout.strip().lower() == "true"

    async def execute_secb(
        self,
        container_id: str,
        command: str,
        timeout_seconds: int = 600,
    ) -> SecbResult:
        """Execute secb command inside container.

        Args:
            container_id: Target container ID.
            command: secb subcommand ("build", "repro", or "patch").
            timeout_seconds: Maximum execution time.

        Returns:
            SecbResult with exit code and output.
        """
        # Get work directory from container info
        work_dir = self._containers.get(container_id)
        workdir = work_dir.work_dir if work_dir else None

        return await self.exec_command(
            container_id=container_id,
            command=f"secb {command}",
            workdir=workdir,
            timeout_seconds=timeout_seconds,
        )

    async def exec_command(
        self,
        container_id: str,
        command: str,
        workdir: str | None = None,
        timeout_seconds: int = 300,
    ) -> SecbResult:
        """Execute arbitrary command inside container.

        Args:
            container_id: Target container ID.
            command: Shell command to execute.
            workdir: Working directory inside container.
            timeout_seconds: Maximum execution time.

        Returns:
            SecbResult with exit code and output.
        """
        cmd = ["docker", "exec"]

        if workdir:
            cmd.extend(["-w", workdir])

        cmd.extend([container_id, "bash", "-c", command])

        start_time = time.monotonic()

        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                self._run_command(cmd),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            duration = time.monotonic() - start_time
            return SecbResult(
                command=command.split()[0] if " " in command else command,
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
                duration_seconds=duration,
            )

        duration = time.monotonic() - start_time

        # Extract secb command type if applicable
        cmd_type = command.split()[1] if command.startswith("secb ") else command.split()[0]

        return SecbResult(
            command=cmd_type,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    async def stream_logs(self, container_id: str) -> AsyncIterator[str]:
        """Stream container logs in real-time.

        Args:
            container_id: Container ID to stream logs from.

        Yields:
            Log lines as they become available.
        """
        cmd = ["docker", "logs", "-f", "--tail", "100", container_id]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        if process.stdout:
            async for line in process.stdout:
                yield line.decode("utf-8", errors="replace").rstrip()

    async def _run_command(
        self,
        cmd: list[str],
    ) -> tuple[int, str, str]:
        """Run shell command and return output.

        Args:
            cmd: Command parts to execute.

        Returns:
            Tuple of (exit_code, stdout, stderr).
        """
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def _install_secb_script(
        self,
        container_id: str,
        secb_content: str,
    ) -> None:
        """Install secb helper script in container.

        Args:
            container_id: Target container ID.
            secb_content: Content of the secb script.
        """
        # Write secb script to /usr/local/bin/secb
        install_cmd = f"""
cat > /usr/local/bin/secb << 'SECB_EOF'
{secb_content}
SECB_EOF
chmod +x /usr/local/bin/secb
"""
        await self.exec_command(container_id, install_cmd)

    async def get_container_info(self, container_id: str) -> ContainerInfo | None:
        """Get container info by ID.

        Args:
            container_id: Container ID to look up.

        Returns:
            ContainerInfo if found, None otherwise.
        """
        return self._containers.get(container_id)

    async def list_secbench_containers(self) -> list[ContainerInfo]:
        """List all SEC-bench containers managed by this adapter.

        Returns:
            List of ContainerInfo for all managed containers.
        """
        return list(self._containers.values())
