"""File system workspace scanner adapter.

Implements WorkspaceScannerPort using standard file system operations.
"""

from pathlib import Path

from core.ports.workspace_scanner_port import WorkspaceScannerPort


class FileSystemWorkspaceScanner(WorkspaceScannerPort):
    """File system implementation of workspace scanning.

    Scans directories for files, optionally excluding hidden files/directories.
    """

    def scan_files(
        self,
        directory: str,
        exclude_hidden: bool = True,
    ) -> list[str]:
        """Scan directory and return sorted list of relative file paths.

        Args:
            directory: Absolute path to directory to scan.
            exclude_hidden: Whether to exclude hidden files/directories.

        Returns:
            Sorted list of relative file paths.
            Empty list if directory doesn't exist or on error.
        """
        dir_path = Path(directory)

        if not dir_path.exists():
            return []

        try:
            files: list[str] = []
            for item in dir_path.rglob("*"):
                # Skip hidden directories and their contents if requested
                if exclude_hidden and any(
                    part.startswith(".") for part in item.parts
                ):
                    continue

                if item.is_file():
                    rel_path = item.relative_to(dir_path)
                    files.append(str(rel_path))

            files.sort()
            return files

        except OSError:
            return []

    def directory_exists(self, directory: str) -> bool:
        """Check if directory exists.

        Args:
            directory: Absolute path to check.

        Returns:
            True if directory exists, False otherwise.
        """
        return Path(directory).exists()
