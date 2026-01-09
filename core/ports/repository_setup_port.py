"""Port for repository setup operations.

Defines the interface for cloning and checking out source code repositories.
Used to prepare working directories with CVE-specific source code before
agent execution.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CloneResult:
    """Result of repository clone operation."""

    success: bool
    repo_path: Path | None = None
    error: str | None = None


class RepositorySetupPort(ABC):
    """Port for setting up source code repository.

    Abstracts repository operations (clone, checkout) to allow different
    implementations (Git, mock for testing, etc.).
    """

    @abstractmethod
    async def clone_and_checkout(
        self,
        repo: str,
        commit: str,
        target_dir: Path,
    ) -> CloneResult:
        """Clone repository and checkout specific commit.

        Args:
            repo: Repository path (e.g., "nginx/njs" for GitHub).
            commit: Commit hash to checkout.
            target_dir: Directory to clone into.

        Returns:
            CloneResult with success status and repo path or error.
        """
        ...
