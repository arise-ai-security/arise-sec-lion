"""Workspace context provider for file listing.

Provides workspace file context to workers so they can see each other's files.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ports.workspace_scanner_port import WorkspaceScannerPort


class WorkspaceContextProvider:
    """Provides fresh workspace file listing for worker context.

    Always scans fresh so workers see files created by other workers.
    Delegates file system operations to WorkspaceScannerPort for DDD compliance.
    """

    def __init__(self, scanner: "WorkspaceScannerPort") -> None:
        """Initialize with workspace scanner.

        Args:
            scanner: Port for file system scanning operations.
        """
        self._scanner = scanner
        self._working_directory: str | None = None

    def set_working_directory(self, path: str | None) -> None:
        """Set the working directory.

        Args:
            path: Absolute path to working directory, or None to clear.
        """
        self._working_directory = str(path) if path is not None else None

    def reset(self) -> None:
        """Reset all state for a new run."""
        self._working_directory = None

    def get_context(self) -> str | None:
        """Return fresh file listing for workspace.

        Always scans fresh so workers can see files created by other workers.
        Returns None if no working directory is set or directory is empty.
        """
        if self._working_directory is None:
            return None

        if not self._scanner.directory_exists(self._working_directory):
            return None

        files = self._scanner.scan_files(
            self._working_directory,
            exclude_hidden=True,
        )

        if not files:
            return None

        return "\n".join(f"- {f}" for f in files)

    @property
    def working_directory(self) -> str | None:
        """Get the current working directory as string."""
        return self._working_directory
