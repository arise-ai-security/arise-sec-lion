"""Agent summary projection service.

Builds AgentSummary read models from events with efficient child loading.
"""

from uuid import UUID

from core.domain.events.events import (
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    StatusChanged,
    SubtasksDefined,
    WorkCompleted,
    WorkFailed,
)
from core.domain.aggregates.agent_session import AgentSession
from core.ports.event_store_port import EventStoreReadPort
from core.query.projections.models import AgentSummary, SubtaskSummary


class AgentSummaryService:
    """Service for building agent summary projections.

    This service encapsulates the logic for:
    - Extracting summary data from agent events
    - Parallel batch loading of child agent statuses
    - Building configuration details
    """

    def __init__(self, event_store: EventStoreReadPort) -> None:
        """Initialize with event store.

        Args:
            event_store: Read-only event store for fetching events.
        """
        self._event_store = event_store

    async def build(self, agent_id: UUID) -> AgentSummary | None:
        """Build agent summary from events.

        Args:
            agent_id: UUID of the agent to summarize.

        Returns:
            AgentSummary if agent exists, None otherwise.
        """
        events = await self._event_store.get_events(agent_id)
        if not events:
            return None

        agent = AgentSession.load_from_history(events)

        # Extract data from events
        summary_data = self._extract_event_data(events)

        # Build subtasks with child statuses (single batch query)
        subtasks = await self._build_subtasks(
            summary_data["subtask_descriptions"],
            summary_data["child_ids"],
            agent_id,  # Pass parent_id for batch query
        )

        # Extract config details
        config_strategy, config_details = self._extract_config(agent)

        return AgentSummary(
            agent_id=agent.agent_id,
            role=agent.role.value,
            status=agent.status.value,
            task_description=agent.task_description or "",
            complexity=summary_data["complexity"],
            complexity_reasoning=summary_data["complexity_reasoning"],
            worker_tool=summary_data["worker_tool"],
            subtasks=subtasks,
            config_strategy=config_strategy,
            config_details=config_details,
            result=agent.result,
            error_message=agent.error_message,
        )

    def _extract_event_data(self, events: list[DomainEvent]) -> dict:
        """Extract summary-relevant data from events.

        Args:
            events: List of domain events for the agent.

        Returns:
            Dict containing complexity, worker_tool, subtasks, and child_ids.
        """
        complexity: str | None = None
        complexity_reasoning: str | None = None
        worker_tool: str | None = None
        subtask_descriptions: list[str] = []
        child_ids: list[UUID] = []

        for event in events:
            if isinstance(event, ComplexityEvaluated):
                complexity = event.complexity
                complexity_reasoning = event.reasoning

            elif isinstance(event, CodeGenerationStarted):
                worker_tool = event.tool_name

            elif isinstance(event, SubtasksDefined):
                subtask_descriptions = [s.description for s in event.subtasks]

            elif isinstance(event, ChildSpawned):
                child_ids.append(event.child_id)

        return {
            "complexity": complexity,
            "complexity_reasoning": complexity_reasoning,
            "worker_tool": worker_tool,
            "subtask_descriptions": subtask_descriptions,
            "child_ids": child_ids,
        }

    async def _build_subtasks(
        self,
        descriptions: list[str],
        child_ids: list[UUID],
        parent_id: UUID,
    ) -> tuple[SubtaskSummary, ...]:
        """Build subtask summaries with child statuses.

        Uses single batch query to avoid N+1 queries.

        Args:
            descriptions: List of subtask descriptions.
            child_ids: List of child agent UUIDs (in subtask order).
            parent_id: Parent agent UUID for batch query.

        Returns:
            Tuple of SubtaskSummary with child info populated.
        """
        if not descriptions:
            return ()

        # Single batch query for all child statuses
        child_statuses = await self._fetch_child_statuses(child_ids, parent_id)

        # Build subtasks with matched child info
        subtasks = []
        for idx, description in enumerate(descriptions):
            child_id = child_ids[idx] if idx < len(child_ids) else None
            child_status = child_statuses.get(child_id) if child_id else None

            subtasks.append(
                SubtaskSummary(
                    description=description,
                    child_id=child_id,
                    child_status=child_status,
                )
            )

        return tuple(subtasks)

    async def _fetch_child_statuses(
        self, child_ids: list[UUID], parent_id: UUID
    ) -> dict[UUID, str]:
        """Fetch statuses for multiple children using single batch query.

        Uses get_children_events_grouped() for single round-trip instead of
        N individual queries for N children.

        Args:
            child_ids: List of child agent UUIDs.
            parent_id: Parent agent UUID for batch query.

        Returns:
            Dict mapping child_id to status string.
        """
        if not child_ids:
            return {}

        # Single batch query fetches all children's events at once
        children_events = await self._event_store.get_children_events_grouped(parent_id)

        result: dict[UUID, str] = {}
        for child_id in child_ids:
            events = children_events.get(child_id, [])
            if not events:
                continue

            # Extract status from events without full aggregate reconstruction
            status = "analyzing"
            for event in events:
                if isinstance(event, StatusChanged):
                    status = event.new_status
                elif isinstance(event, CodeGenerationStarted):
                    status = "in_progress"
                elif isinstance(event, WorkCompleted):
                    status = "completed"
                elif isinstance(event, WorkFailed):
                    status = "failed"
            result[child_id] = status

        return result

    def _extract_config(self, agent: AgentSession) -> tuple[str | None, dict]:
        """Extract configuration details from agent.

        Args:
            agent: The agent session.

        Returns:
            Tuple of (config_strategy, config_details dict).
        """
        if not hasattr(agent, "config") or not agent.config:
            return None, {}

        config = agent.config
        strategy = getattr(config, "strategy", None)
        details = self._build_config_details(config, strategy)
        return strategy, details

    def _build_config_details(self, config: object, strategy: str | None) -> dict:
        """Build config details dict based on strategy type.

        Args:
            config: Agent configuration object.
            strategy: Configuration strategy name.

        Returns:
            Dict with strategy-specific configuration details.
        """
        if strategy == "per_operation":
            return {
                "complexity_evaluation": {
                    "model": config.complexity_evaluation.model,
                    "temperature": config.complexity_evaluation.temperature,
                    "max_tokens": config.complexity_evaluation.max_tokens,
                },
                "task_decomposition": {
                    "model": config.task_decomposition.model,
                    "temperature": config.task_decomposition.temperature,
                    "max_tokens": config.task_decomposition.max_tokens,
                },
                "tool": config.tool,
            }
        if strategy == "heuristic":
            return {
                "base": {
                    "model": config.base.model,
                    "temperature": config.base.temperature,
                    "max_tokens": config.base.max_tokens,
                },
                "tool": config.tool,
            }
        if strategy == "hybrid":
            return {
                "base": {
                    "model": config.base.model,
                    "temperature": config.base.temperature,
                    "max_tokens": config.base.max_tokens,
                },
                "overrides": config.overrides,
                "tool": config.tool,
            }
        return {}
