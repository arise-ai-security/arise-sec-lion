"""Workspace scanner port for file listing operations.

Abstracts file system scanning to maintain hexagonal architecture.
Application layer uses this port; infrastructure provides implementation.
"""

from typing import Protocol


class WorkspaceScannerPort(Protocol):
    """Port for scanning workspace directories.

    Provides file listing capabilities without exposing file system details
    to the application layer.
    """

    def scan_files(
        self,
        directory: str,
        exclude_hidden: bool = True,
    ) -> list[str]:
        """Scan directory and return list of relative file paths.

        Args:
            directory: Absolute path to directory to scan.
            exclude_hidden: Whether to exclude hidden files/directories.

        Returns:
            List of relative file paths (sorted).
            Empty list if directory doesn't exist or is empty.
        """
        ...

    def directory_exists(self, directory: str) -> bool:
        """Check if directory exists.

        Args:
            directory: Absolute path to check.

        Returns:
            True if directory exists, False otherwise.
        """
        ...
