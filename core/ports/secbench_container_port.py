"""Port for SEC-bench container lifecycle management.

Follows Hexagonal Architecture:
- Port defines the contract (abstraction)
- Implementation in infrastructure/adapters/secbench/docker_adapter.py
- Depends on core/domain types only
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance


@dataclass(frozen=True)
class ContainerInfo:
    """Immutable container state snapshot."""

    container_id: str
    image: str
    status: str  # "created", "running", "stopped", "removed"
    work_dir: str


@dataclass(frozen=True)
class SecbResult:
    """Result of secb command execution."""

    command: str  # "build", "repro", "patch"
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def success(self) -> bool:
        """Check if command succeeded (exit code 0)."""
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined stdout and stderr."""
        return self.stdout + self.stderr


class SecBenchContainerPort(Protocol):
    """Port for managing SEC-bench Docker container lifecycle.

    Used by ExecutionService to start containers for in-container
    worker execution during SEC-bench benchmark runs.

    Implementations should use Docker-out-of-Docker (DooD) pattern
    where the Arise container communicates with the host Docker daemon
    via mounted /var/run/docker.sock.
    """

    async def start_container(
        self,
        cve: "CVEInstance",
        testcase_path: Path,
        root_id: UUID,
    ) -> ContainerInfo:
        """Start SEC-bench container with testcase volume mount.

        The container is started with:
        - The pre-built SEC-bench Docker image for the CVE
        - /testcase volume mounted for artifact sharing
        - secb helper script installed from CVEInstance.secb_sh

        Args:
            cve: CVE instance containing docker_image and secb_sh.
            testcase_path: Local path to mount as /testcase in container.
            root_id: Root agent ID for tracking/labeling.

        Returns:
            ContainerInfo with container_id and status.

        Raises:
            RuntimeError: If container fails to start.
        """
        ...

    async def stop_container(self, container_id: str) -> None:
        """Stop and remove container.

        Args:
            container_id: Container ID to stop.
        """
        ...

    async def is_running(self, container_id: str) -> bool:
        """Check if container is still running.

        Args:
            container_id: Container ID to check.

        Returns:
            True if container is running, False otherwise.
        """
        ...

    async def execute_secb(
        self,
        container_id: str,
        command: str,
        timeout_seconds: int = 600,
    ) -> SecbResult:
        """Execute secb command inside container.

        Maps to SEC-bench paper commands:
        - secb build: Build the vulnerable project
        - secb repro: Execute PoC and capture sanitizer output
        - secb patch: Apply and verify patch

        Args:
            container_id: Target container ID.
            command: secb subcommand ("build", "repro", or "patch").
            timeout_seconds: Maximum execution time.

        Returns:
            SecbResult with exit code and output.
        """
        ...

    async def exec_command(
        self,
        container_id: str,
        command: str,
        workdir: str | None = None,
        timeout_seconds: int = 300,
    ) -> SecbResult:
        """Execute arbitrary command inside container.

        Utility method for running commands in the container.

        Args:
            container_id: Target container ID.
            command: Shell command to execute.
            workdir: Working directory inside container (optional).
            timeout_seconds: Maximum execution time.

        Returns:
            SecbResult with exit code and output.
        """
        ...

    def stream_logs(self, container_id: str) -> AsyncIterator[str]:
        """Stream container logs in real-time.

        Args:
            container_id: Container ID to stream logs from.

        Yields:
            Log lines as they become available.
        """
        ...
