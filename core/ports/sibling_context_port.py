"""Port for building sibling context for workers.

Follows Hexagonal Architecture:
- Port defines the contract (abstraction)
- Implementation in core/application/services/sibling_context_builder.py
- Depends on core/domain types only
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.values.sibling_context import WorkerSiblingContext


class SiblingContextPort(Protocol):
    """Port for building sibling context for workers.

    Follows Interface Segregation Principle:
    - Read-only, no side effects
    - Single method for building context
    """

    async def build_context(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
        root_id: UUID,
    ) -> WorkerSiblingContext:
        """Build sibling context for a worker.

        Retrieves:
        - Parent task description (minimal context)
        - Sibling task info (statuses, results)
        - Shared decisions from SharedExecutionContext

        Args:
            agent_id: Current worker's agent ID (excluded from sibling list)
            parent_id: Parent agent ID (to find siblings with same parent)
            root_id: Root agent ID (to load shared decisions)

        Returns:
            WorkerSiblingContext with sibling info and shared decisions
        """
        ...
