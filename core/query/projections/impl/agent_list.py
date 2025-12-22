"""Agent list projection - lightweight read model for agent listings.

This projection builds AgentListItem read models directly from events
without reconstructing the full AgentSession aggregate.
"""

from uuid import UUID

from core.domain.events import (
    AgentCreated,
    ChildSpawned,
    DomainEvent,
    StatusChanged,
    TaskAssigned,
)
from core.query.projections.models import AgentListItem


class AgentListProjection:
    """Projection that builds AgentListItem from events.

    This is a lightweight CQRS read model that only extracts the fields
    needed for agent listings, avoiding full aggregate reconstruction.
    """

    def project(self, events: list[DomainEvent]) -> AgentListItem | None:
        """Project events into an AgentListItem.

        Args:
            events: List of domain events for a single aggregate.

        Returns:
            AgentListItem if events contain AgentCreated, None otherwise.
        """
        if not events:
            return None

        # Skip non-agent aggregates (e.g., SharedExecutionContext)
        first_event = events[0]
        if not isinstance(first_event, AgentCreated):
            return None

        # Extract data from events
        agent_id = first_event.aggregate_id
        role = first_event.role
        parent_id = first_event.parent_id
        created_at = first_event.occurred_at

        # Default status
        status = "analyzing"

        # Track task description and child IDs
        task_description: str | None = None
        child_ids: list[UUID] = []

        for event in events:
            if isinstance(event, TaskAssigned):
                task_description = event.task_description

            elif isinstance(event, StatusChanged):
                status = event.new_status

            elif isinstance(event, ChildSpawned):
                child_ids.append(event.child_id)

        return AgentListItem(
            agent_id=agent_id,
            role=role,
            status=status,
            task_description=task_description,
            parent_id=parent_id,
            created_at=created_at,
            child_ids=tuple(child_ids),
        )

    def project_all(
        self, grouped_events: dict[UUID, list[DomainEvent]]
    ) -> dict[UUID, AgentListItem]:
        """Project all aggregates into AgentListItems.

        Args:
            grouped_events: Dict mapping aggregate_id to events.

        Returns:
            Dict mapping agent_id to AgentListItem (only agents, not other aggregates).
        """
        result: dict[UUID, AgentListItem] = {}

        for agent_id, events in grouped_events.items():
            item = self.project(events)
            if item is not None:
                result[agent_id] = item

        return result

    def filter_boss_agents(
        self, agents: dict[UUID, AgentListItem]
    ) -> list[AgentListItem]:
        """Filter to only BOSS (root) agents without parents.

        Args:
            agents: Dict of all agent list items.

        Returns:
            List of BOSS agents sorted by creation time (newest first).
        """
        boss_agents = [agent for agent in agents.values() if agent.parent_id is None]

        # Sort by creation time (newest first)
        boss_agents.sort(
            key=lambda a: a.created_at or "",
            reverse=True,
        )

        return boss_agents
