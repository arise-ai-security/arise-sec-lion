"""Workspace context provider for file listing.

Provides workspace file context to workers so they can see each other's files.
"""

from __future__ import annotations

from pathlib import Path


class WorkspaceContextProvider:
    """Provides cached workspace file listing for worker context.

    Single Responsibility: Scan and cache workspace file structure.
    """

    def __init__(self) -> None:
        self._cache: str | None = None
        self._scanned: bool = False
        self._working_directory: Path | None = None

    def set_working_directory(self, path: str | Path | None) -> None:
        """Set the working directory and invalidate cache."""
        if path is None:
            self._working_directory = None
        else:
            self._working_directory = Path(path)
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        """Invalidate the cached context."""
        self._cache = None
        self._scanned = False

    def reset(self) -> None:
        """Reset all state for a new run."""
        self._cache = None
        self._scanned = False
        self._working_directory = None

    def get_context(self) -> str | None:
        """Return cached file listing for workspace.

        Enables workers to see each other's files.
        Returns None if no working directory is set or directory is empty.
        """
        if self._scanned:
            return self._cache

        self._scanned = True

        if self._working_directory is None:
            return None

        if not self._working_directory.exists():
            return None

        try:
            files = self._scan_files()
            if not files:
                return None

            files.sort()
            self._cache = "\n".join(f"- {f}" for f in files)
            return self._cache

        except OSError:
            return None

    def _scan_files(self) -> list[str]:
        """Scan workspace for files, excluding hidden directories."""
        if self._working_directory is None:
            return []

        files = []
        for item in self._working_directory.rglob("*"):
            # Skip hidden directories and their contents
            if any(part.startswith(".") for part in item.parts):
                continue
            if item.is_file():
                rel_path = item.relative_to(self._working_directory)
                files.append(str(rel_path))

        return files

    @property
    def working_directory(self) -> str | None:
        """Get the current working directory as string."""
        return str(self._working_directory) if self._working_directory else None
