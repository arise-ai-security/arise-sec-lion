"""SEC-bench container runtime contracts and value objects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from core.ports.domain_plugin_port import SealedRuntimeSurface, WorkspacePathAlias
from plugins.security.cve_instance import CVEInstance


@dataclass(frozen=True)
class SecBenchWorkspace:
    """Host workspace that mirrors the container filesystem.

    ``container_workspace_root`` is the in-container mount point for
    ``host_root``; the runtime bind-mounts the workspace root so worker
    scratch files (e.g. ``mcp_servers.json``, scratch ``CLAUDE_CONFIG_DIR``)
    written on the host are also reachable from inside the container.
    """

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
    container_work_dir: str = "/work"
    container_workspace_root: str = "/arise-run"
    sealed_surface: SealedRuntimeSurface | None = None

    @property
    def host_work_root(self) -> Path:
        """Host mirror for container `/work` build and runtime artifacts."""
        return self.host_root / "work"

    def path_aliases(self) -> tuple[WorkspacePathAlias, ...]:
        """Canonical container paths mapped to this run's host mirror."""
        return (
            WorkspacePathAlias(self.container_source_dir, str(self.host_source_dir)),
            WorkspacePathAlias(self.container_testcase_dir, str(self.host_testcase_dir)),
            WorkspacePathAlias(self.container_work_dir, str(self.host_work_root)),
        )


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
            "host_artifact_dir": str(self.workspace.host_testcase_dir),
            "host_work_root": str(self.workspace.host_work_root),
            "host_work_dir": str(self.workspace.host_work_dir),
            "container_source_dir": self.workspace.container_source_dir,
            "container_artifact_dir": self.workspace.container_testcase_dir,
            "container_work_dir": self.workspace.container_work_dir,
            "container_working_directory": self.workspace.container_working_directory,
            "container_workspace_root": self.workspace.container_workspace_root,
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
        ...

    async def start_session(
        self,
        cve: CVEInstance,
        workspace: SecBenchWorkspace,
        agent_id: UUID,
    ) -> SecBenchContainerSession:
        """Start a container bound to the prepared workspace."""
        ...
