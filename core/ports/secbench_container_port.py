"""Port for SEC-bench container lifecycle management.

Follows Hexagonal Architecture:
- Port defines the contract (abstraction)
- Implementation in infrastructure/adapters/secbench_container.py
- Depends on core/domain types only
"""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance


class SecBenchContainerPort(Protocol):
    """Port for managing SEC-bench Docker container lifecycle.

    Used by ExecutionService to start containers for in-container
    worker execution during SEC-bench benchmark runs.
    """

    async def start_container(
        self,
        cve: "CVEInstance",
        testcase_path: Path,
    ) -> str:
        """Start SEC-bench container with testcase volume mount.

        Args:
            cve: CVE instance containing docker_image and secb_sh.
            testcase_path: Local path to mount as /testcase in container.

        Returns:
            Container ID (short form).
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
