"""Git-based repository setup adapter.

Implements RepositorySetupPort using Git CLI for cloning and checkout operations.
"""

import asyncio
import logging
from pathlib import Path

from core.ports.repository_setup_port import CloneResult, RepositorySetupPort

logger = logging.getLogger(__name__)


class GitRepositoryAdapter(RepositorySetupPort):
    """Git-based repository setup using subprocess.

    Clones repositories from GitHub and checks out specific commits
    for CVE vulnerability analysis.
    """

    async def clone_and_checkout(
        self,
        repo: str,
        commit: str,
        target_dir: Path,
    ) -> CloneResult:
        """Clone GitHub repository and checkout specific commit.

        Args:
            repo: Repository path (e.g., "nginx/njs").
            commit: Commit hash to checkout.
            target_dir: Directory to clone into.

        Returns:
            CloneResult with success status and repo path or error.
        """
        url = f"https://github.com/{repo}.git"

        try:
            # Clone repository (full clone needed for commit checkout)
            logger.info("Cloning %s to %s", url, target_dir)
            await self._run_git(["clone", url, str(target_dir)])

            # Checkout specific commit
            logger.info("Checking out commit %s", commit)
            await self._run_git(["-C", str(target_dir), "checkout", commit])

            logger.info("Repository setup complete: %s @ %s", repo, commit[:8])
            return CloneResult(success=True, repo_path=target_dir)

        except Exception as e:
            logger.error("Failed to clone repository %s: %s", repo, e)
            return CloneResult(success=False, error=str(e))

    async def _run_git(self, args: list[str], timeout_seconds: int = 300) -> None:
        """Execute git command asynchronously with timeout.

        Args:
            args: Git command arguments (excluding 'git' itself).
            timeout_seconds: Maximum time to wait for command completion.

        Raises:
            RuntimeError: If git command fails or times out.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"git {args[0]} timed out after {timeout_seconds}s")

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"git {args[0]} failed: {error_msg}")
