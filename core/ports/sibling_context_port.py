"""Port for building sibling view for workers.

Follows Hexagonal Architecture:
- Port defines the contract (abstraction)
- Implementation in core/application/services/sibling_context_builder.py
- Depends on core/domain types only
"""

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.values.context import SiblingView


class SiblingViewPort(Protocol):
    """Port for building sibling view for workers.

    Follows Interface Segregation Principle:
    - Read-only, no side effects
    - Single method for building view
    """

    async def build_view(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
        root_id: UUID,
    ) -> SiblingView:
        """Build sibling view for a worker.

        Retrieves:
        - Parent task description (minimal context)
        - Sibling statuses (progress, results)
        - Shared decisions from SharedExecutionContext

        Args:
            agent_id: Current worker's agent ID (excluded from sibling list)
            parent_id: Parent agent ID (to find siblings with same parent)
            root_id: Root agent ID (to load shared decisions)

        Returns:
            SiblingView with sibling statuses and shared decisions
        """
        ...
