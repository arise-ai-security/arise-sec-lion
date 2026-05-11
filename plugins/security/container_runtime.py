"""SEC-bench container runtime contracts and value objects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from plugins.security.cve_instance import CVEInstance


@dataclass(frozen=True)
class SecBenchWorkspace:
    """Host workspace that mirrors the container filesystem."""

    root_id: UUID
    image: str
    host_root: Path
    host_source_dir: Path
    host_testcase_dir: Path
    host_work_dir: Path
    container_source_dir: str
    container_testcase_dir: str
    container_working_directory: str
    helper_script: Path


@dataclass(frozen=True)
class SecBenchContainerSession:
    """Active SEC-bench worker container bound to a host workspace."""

    workspace: SecBenchWorkspace
    container_id: str
    container_name: str
    image: str

    def to_task_context(self) -> dict[str, str]:
        """Serialize session metadata for worker adapters."""
        return {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "image": self.image,
            "workspace_root": str(self.workspace.host_root),
            "host_source_dir": str(self.workspace.host_source_dir),
            "host_testcase_dir": str(self.workspace.host_testcase_dir),
            "host_work_dir": str(self.workspace.host_work_dir),
            "container_source_dir": self.workspace.container_source_dir,
            "container_testcase_dir": self.workspace.container_testcase_dir,
            "container_working_directory": self.workspace.container_working_directory,
            "helper_script": str(self.workspace.helper_script),
        }


class SecurityContainerRuntime(Protocol):
    """Runtime contract used by the security plugin."""

    async def prepare_workspace(
        self,
        cve: CVEInstance,
        run_output_path: Path,
        image: str,
        root_id: UUID,
    ) -> SecBenchWorkspace:
        """Create the host workspace that mirrors the container image."""
        ...

    async def start_session(
        self,
        cve: CVEInstance,
        workspace: SecBenchWorkspace,
        agent_id: UUID,
    ) -> SecBenchContainerSession:
        """Start a container bound to the prepared workspace."""
        ...

    async def is_session_alive(self, session: SecBenchContainerSession) -> bool:
        """Return True if the container is still running."""
        ...

    async def stop_session(self, session: SecBenchContainerSession) -> None:
        """Stop and remove the active worker container."""
        ...
