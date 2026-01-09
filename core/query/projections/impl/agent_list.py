"""Agent list projection - lightweight read model for agent listings.

This projection builds AgentListItem read models directly from events
without reconstructing the full AgentSession aggregate.
"""

from dataclasses import dataclass, field
from datetime import datetime
from functools import singledispatchmethod
from uuid import UUID

from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    RoleTransitioned,
    StatusChanged,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)
from core.query.projections.models import AgentListItem

# Terminal states should not be overwritten
TERMINAL_STATES = frozenset({"completed", "failed"})


@dataclass
class _ProjectionState:
    """Mutable state accumulated during event projection."""

    agent_id: UUID
    role: str
    parent_id: UUID | None
    created_at: datetime
    status: str = "analyzing"
    task_description: str | None = None
    child_ids: list[UUID] = field(default_factory=list)

    def to_item(self) -> AgentListItem:
        """Convert accumulated state to immutable AgentListItem."""
        return AgentListItem(
            agent_id=self.agent_id,
            role=self.role,
            status=self.status,
            task_description=self.task_description,
            parent_id=self.parent_id,
            created_at=self.created_at,
            child_ids=tuple(self.child_ids),
        )


class AgentListProjection:
    """Projection that builds AgentListItem from events.

    This is a lightweight CQRS read model that only extracts the fields
    needed for agent listings, avoiding full aggregate reconstruction.

    Uses singledispatchmethod for extensible event handling - new events
    can be supported by adding @_apply.register methods.
    """

    @singledispatchmethod
    def _apply(self, event: DomainEvent, state: _ProjectionState) -> None:
        """Apply an event to projection state. Unknown events are ignored."""
        pass

    @_apply.register
    def _(self, event: TaskAssigned, state: _ProjectionState) -> None:
        state.task_description = event.task_description

    @_apply.register
    def _(self, event: StatusChanged, state: _ProjectionState) -> None:
        if state.status not in TERMINAL_STATES:
            state.status = event.new_status

    @_apply.register
    def _(self, event: ComplexityEvaluated, state: _ProjectionState) -> None:
        state.role = event.determined_role

    @_apply.register
    def _(self, event: RoleTransitioned, state: _ProjectionState) -> None:
        state.role = event.to_role

    @_apply.register
    def _(self, event: CodeGenerationStarted, state: _ProjectionState) -> None:
        if state.status not in TERMINAL_STATES:
            state.status = "in_progress"

    @_apply.register
    def _(self, event: WorkCompleted, state: _ProjectionState) -> None:
        state.status = "completed"

    @_apply.register
    def _(self, event: WorkFailed, state: _ProjectionState) -> None:
        state.status = "failed"

    @_apply.register
    def _(self, event: ChildSpawned, state: _ProjectionState) -> None:
        state.child_ids.append(event.child_id)

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

        # Initialize state from AgentCreated event
        state = _ProjectionState(
            agent_id=first_event.aggregate_id,
            role=first_event.role,
            parent_id=first_event.parent_id,
            created_at=first_event.occurred_at,
        )

        # Apply remaining events
        for event in events[1:]:
            self._apply(event, state)

        return state.to_item()

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
