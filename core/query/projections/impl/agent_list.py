"""Agent list projection - lightweight read model for agent listings.

This projection builds AgentListItem read models directly from events
without reconstructing the full AgentSession aggregate.
"""

from uuid import UUID

from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DecisionInfeasible,
    DomainEvent,
    RedecompositionTriggered,
    RetryScheduled,
    RunStarted,
    StatusChanged,
    TaskAssigned,
    VerificationFailed,
    WorkCompleted,
    WorkFailed,
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

        # Skip non-agent aggregates (e.g., SharedStore)
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

        # Terminal states should not be overwritten
        terminal_states = {"completed", "failed"}

        # Track task description and child IDs
        task_description: str | None = None
        domain_metadata = None
        child_ids: list[UUID] = []
        restart_count = 0
        hang_restart_count = 0

        for event in events:
            if isinstance(event, TaskAssigned):
                task_description = event.task_description
            elif isinstance(event, RunStarted):
                domain_metadata = event.domain_metadata
            elif isinstance(event, StatusChanged):
                # Only update if not already in terminal state
                if status not in terminal_states:
                    status = event.new_status

            elif isinstance(event, ComplexityEvaluated):
                # Update role based on complexity evaluation (pending → worker/manager)
                role = event.determined_role

            elif isinstance(event, CodeGenerationStarted):
                if status not in terminal_states:
                    status = "in_progress"

            elif isinstance(event, WorkCompleted):
                status = "completed"

            elif isinstance(event, WorkFailed) or isinstance(event, VerificationFailed) or isinstance(event, DecisionInfeasible):
                status = "failed"

            elif isinstance(event, RetryScheduled):
                # FAILED → ANALYZING: agent is retrying, no longer terminal
                status = "analyzing"
                restart_count += 1
                if self._is_hang_recovery_reason(event.reason):
                    hang_restart_count += 1

            elif isinstance(event, RedecompositionTriggered):
                # WAITING → ANALYZING: parent re-decomposes after child infeasible
                status = "analyzing"

            elif isinstance(event, ChildSpawned):
                child_ids.append(event.child_id)

        return AgentListItem(
            agent_id=agent_id,
            role=role,
            status=status,
            task_description=task_description,
            parent_id=parent_id,
            created_at=created_at,
            domain_metadata=domain_metadata,
            child_ids=tuple(child_ids),
            restart_count=restart_count,
            was_restarted=restart_count > 0,
            hang_restart_count=hang_restart_count,
            was_hang_restarted=hang_restart_count > 0,
        )

    @staticmethod
    def _is_hang_recovery_reason(reason: str) -> bool:
        """Classify retry reasons that represent hang/no-progress recovery."""
        if not reason:
            return False
        normalized = reason.lower()
        return any(
            marker in normalized
            for marker in (
                "zero thoughts after",
                "step timed out after",
                "worker silent for >",
                "pending assessment timed out after",
                "no-progress",
                "llm likely unresponsive",
            )
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
