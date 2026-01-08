"""Container manager service for SEC-bench lifecycle orchestration.

Manages container lifecycle with in-memory tracking of active containers.
"""

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from core.ports.secbench_container_port import ContainerInfo, SecBenchContainerPort

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance


class ContainerManager:
    """Orchestrates SEC-bench container lifecycle.

    Responsibilities:
    - Start/stop containers for SEC-bench CVE instances
    - Track active containers in memory
    - Provide container info to prompt builders
    - Print verification commands on completion
    """

    def __init__(
        self,
        container_port: SecBenchContainerPort,
        output_directory: Path,
        keep_running: bool = True,
    ):
        """Initialize container manager.

        Args:
            container_port: Port for container operations.
            output_directory: Directory for testcase files.
            keep_running: Keep container running after execution.
        """
        self._container_port = container_port
        self._output_dir = output_directory
        self._keep_running = keep_running
        self._active_containers: dict[UUID, ContainerInfo] = {}

    async def start_container_for_cve(
        self,
        cve: "CVEInstance",
        root_id: UUID,
    ) -> ContainerInfo:
        """Start container for a CVE instance and store in SharedContext.

        Args:
            cve: CVE instance to start container for.
            root_id: Root agent ID (BOSS) for tracking.

        Returns:
            ContainerInfo with container details.
        """
        # Create testcase directory for this run
        testcase_path = self._output_dir / str(root_id) / "testcase"
        testcase_path.mkdir(parents=True, exist_ok=True)

        # Start the container
        container_info = await self._container_port.start_container(
            cve=cve,
            testcase_path=testcase_path,
            root_id=root_id,
        )

        # Track active container in memory
        self._active_containers[root_id] = container_info

        return container_info

    async def stop_container(
        self,
        root_id: UUID,
        force: bool = False,
    ) -> None:
        """Stop container for a root agent.

        Args:
            root_id: Root agent ID to stop container for.
            force: Force stop even if keep_running is True.
        """
        if self._keep_running and not force:
            # Don't stop - user will stop manually
            return

        container_info = self._active_containers.get(root_id)
        if container_info:
            await self._container_port.stop_container(container_info.container_id)
            del self._active_containers[root_id]

    async def get_container_info(self, root_id: UUID) -> ContainerInfo | None:
        """Get container info for a root agent.

        Args:
            root_id: Root agent ID to get container for.

        Returns:
            ContainerInfo if container exists, None otherwise.
        """
        return self._active_containers.get(root_id)

    def get_container_id_from_context(self, root_id: UUID) -> str | None:
        """Get container ID from in-memory tracking.

        This is the primary method for prompt builders to get container_id.

        Args:
            root_id: Root agent ID to look up.

        Returns:
            Container ID string if found, None otherwise.
        """
        container_info = self._active_containers.get(root_id)
        return container_info.container_id if container_info else None

    def print_verification_commands(
        self,
        container_info: ContainerInfo,
        instance_id: str,
    ) -> str:
        """Generate verification commands for CLI output.

        Args:
            container_info: Container information.
            instance_id: CVE instance ID.

        Returns:
            Formatted verification commands string.
        """
        container_id = container_info.container_id

        return f"""
============================================================
SEC-bench Container Verification
============================================================
Container ID: {container_id}
Instance: {instance_id}

Verification commands:
  docker exec {container_id} secb build
  docker exec {container_id} secb repro
  docker exec {container_id} secb patch

Manual verification:
  docker exec -it {container_id} bash

Stop when done:
  docker stop {container_id} && docker rm {container_id}
============================================================
"""

    @property
    def active_containers(self) -> dict[UUID, ContainerInfo]:
        """Get all active containers."""
        return self._active_containers.copy()
