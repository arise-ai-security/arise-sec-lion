"""Shared Context Port - interface for shared context persistence.

Follows Interface Segregation Principle with separate reader/writer protocols.
"""

from typing import Protocol
from uuid import UUID

from core.domain.shared_context import Artifact, Decision, SharedExecutionContext


class ContextReaderPort(Protocol):
    """Read-only shared context access.

    Interface segregation: Agents that only need to read context
    depend on this minimal interface.
    """

    async def get_shared_context(self, root_id: UUID) -> SharedExecutionContext | None:
        """Get shared context by root ID.

        Args:
            root_id: Root agent ID (aggregate ID)

        Returns:
            SharedExecutionContext if exists, None otherwise
        """
        ...

    async def get_artifact(self, root_id: UUID, key: str) -> Artifact | None:
        """Get artifact from shared context.

        Args:
            root_id: Root agent ID
            key: Artifact key

        Returns:
            Artifact if exists, None otherwise
        """
        ...

    async def get_decision(self, root_id: UUID, key: str) -> Decision | None:
        """Get decision from shared context.

        Args:
            root_id: Root agent ID
            key: Decision key

        Returns:
            Decision if exists, None otherwise
        """
        ...


class ContextWriterPort(Protocol):
    """Write-only shared context access.

    Interface segregation: Operations that modify context
    depend on this interface.
    """

    async def save_shared_context(
        self,
        context: SharedExecutionContext,
        expected_version: int,
    ) -> None:
        """Save shared context with OCC.

        Args:
            context: The context to save
            expected_version: Expected version for OCC

        Raises:
            ConcurrencyError: If version mismatch
        """
        ...

    async def store_artifact_content(
        self,
        root_id: UUID,
        key: str,
        content: bytes,
    ) -> str:
        """Store large artifact content externally.

        For artifacts too large to store inline in events,
        stores content and returns a hash reference.

        Args:
            root_id: Root agent ID
            key: Artifact key
            content: Binary content to store

        Returns:
            Content hash (SHA256) for reference
        """
        ...


class SharedContextPort(Protocol):
    """Combined interface for shared context operations.

    Primary interface for infrastructure adapters.
    Combines read and write operations.
    """

    async def get_or_create(
        self,
        root_id: UUID,
        config: dict | None = None,
    ) -> SharedExecutionContext:
        """Get existing context or create new one.

        Args:
            root_id: Root agent ID
            initial_budget_usd: Budget limit for new context (0 = unlimited)
            config: Configuration for new context

        Returns:
            Existing or newly created SharedExecutionContext
        """
        ...

    async def get(self, root_id: UUID) -> SharedExecutionContext | None:
        """Get shared context by root ID.

        Args:
            root_id: Root agent ID

        Returns:
            SharedExecutionContext if exists, None otherwise
        """
        ...

    async def save(
        self,
        context: SharedExecutionContext,
        expected_version: int,
    ) -> None:
        """Save shared context with OCC.

        Persists all uncommitted events from the context.

        Args:
            context: The context to save
            expected_version: Expected version for OCC

        Raises:
            ConcurrencyError: If version mismatch
        """
        ...

    async def exists(self, root_id: UUID) -> bool:
        """Check if shared context exists.

        Args:
            root_id: Root agent ID

        Returns:
            True if context exists
        """
        ...
