"""Run-scoped workspace setup and listing for agent execution.

Owns the working-directory pointer for a run: materializes the run-scoped
output directory, delegates plugin-specific preparation, points recon tooling
at the resolved directory, and renders the ``<workspace>`` file listing that
workers see. Extracted from ``AgentExecutionService`` so the service keeps only
the loop and dispatch responsibilities.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from uuid import UUID

    from core.domain.values.json_types import JsonObject
    from core.ports.domain_plugin_port import DomainPlugin, PreparedRunWorkspace
    from core.ports.runtime_ports import ReconToolPort


class WorkspaceContextProvider:
    """Resolve and expose the run-scoped working directory and file listing."""

    # Top-level directories that contain the target project source tree.
    # Workers access source code inside their container, not via the
    # workspace listing, so including these would bloat the prompt with
    # tens of thousands of irrelevant paths.
    _WORKSPACE_SKIP_DIRS: frozenset[str] = frozenset({"src"})

    def __init__(
        self,
        *,
        output_directory: str,
        workspace_listing_dirs: tuple[str, ...] | None,
        workspace_listing_max_entries: int | None,
        domain_plugin: DomainPlugin | None,
        recon_tool: ReconToolPort | None,
    ) -> None:
        self._output_directory = output_directory
        self._workspace_listing_dirs = workspace_listing_dirs
        self._workspace_listing_max_entries = workspace_listing_max_entries
        self._domain_plugin = domain_plugin
        self._recon_tool = recon_tool
        self._working_directory: Path | None = None

    @property
    def working_directory(self) -> Path | None:
        return self._working_directory

    @working_directory.setter
    def working_directory(self, value: Path | None) -> None:
        self._working_directory = value

    def get_run_metadata(self, domain_context: object | None) -> JsonObject | None:
        if self._domain_plugin is None or domain_context is None:
            return None

        metadata = self._domain_plugin.get_run_metadata(domain_context)
        return metadata or None

    async def setup_working_directory(
        self,
        root_id: UUID,
        domain_context: object | None = None,
    ) -> PreparedRunWorkspace | None:
        """Setup working directory for the run."""
        if not self._output_directory:
            return None

        base_output = Path(self._output_directory).resolve()
        base_output.mkdir(parents=True, exist_ok=True)

        run_output_path = base_output / str(root_id)
        run_output_path.mkdir(parents=True, exist_ok=True)

        self._working_directory = run_output_path
        if self._domain_plugin is None:
            return None

        prepared = await self._domain_plugin.prepare_run(
            root_id=root_id,
            run_output_path=run_output_path,
            domain_context=domain_context,
        )
        if prepared is not None and prepared.working_directory:
            self._working_directory = Path(prepared.working_directory)

        self._configure_recon_workspace(prepared)
        return prepared

    def _configure_recon_workspace(
        self,
        prepared: PreparedRunWorkspace | None,
    ) -> None:
        if self._recon_tool is None or self._working_directory is None:
            return

        set_wd = getattr(self._recon_tool, "set_working_directory", None)
        if callable(set_wd):
            set_wd(str(self._working_directory))

        set_aliases = getattr(self._recon_tool, "set_path_aliases", None)
        if callable(set_aliases):
            aliases = prepared.path_aliases if prepared is not None else ()
            set_aliases(aliases)

        logger.info("Recon tools targeting: %s", self._working_directory)

    def get_workspace_context(self) -> str | None:
        """Return fresh file listing for workspace.

        Always scans fresh so workers can see files created by other workers.
        With ``workspace_listing_dirs`` set, only those top-level directories
        are listed (artifact discovery without build-tree noise: an out-of-tree
        CMake build under work/ alone is ~650 object/Makefile entries). The
        legacy mode skips just the source tree (``src/``), which is accessed
        inside the container — only worker-produced artifacts matter here.
        """
        if self._working_directory is None or not self._working_directory.exists():
            return None

        include_dirs = self._workspace_listing_dirs

        try:
            files = []
            for item in self._working_directory.rglob("*"):
                if any(part.startswith(".") for part in item.parts):
                    continue
                rel_path = item.relative_to(self._working_directory)
                if not rel_path.parts:
                    continue
                if include_dirs is not None:
                    if rel_path.parts[0] not in include_dirs:
                        continue
                elif rel_path.parts[0] in self._WORKSPACE_SKIP_DIRS:
                    continue
                if item.is_file():
                    files.append(str(rel_path))

            if not files:
                return None

            files.sort()
            max_entries = self._workspace_listing_max_entries
            if max_entries is not None and len(files) > max_entries:
                overflow = len(files) - max_entries
                files = [*files[:max_entries], f"… (+{overflow} more files)"]
            return "\n".join(f"- {f}" for f in files)
        except OSError:
            return None

    def get_working_directory_str(self) -> str | None:
        return str(self._working_directory) if self._working_directory else None
