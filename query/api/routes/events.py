"""Event-related API routes.

Provides endpoints for querying events and SSE streaming.
SSE endpoints use incremental fetching to reduce database load.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse


logger = logging.getLogger(__name__)

from core.domain.events.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    PromptSent,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.ports.event_store_port import EventStoreReadPort
from core.query.projections.hierarchy_collector import HierarchyCollector
from core.query.projections.impl import SummaryProjection
from query.api.dependencies import EventStoreDep
from query.api.routes.agents import _projection_summary_to_schema
from query.api.schemas import (
    AgentPromptSchema,
    AgentPromptsSchema,
    CategorizedEventsSchema,
    EventSchema,
)


router = APIRouter()

# Event categorization mapping
RECEIVED_EVENTS = {TaskAssigned}
PRODUCED_EVENTS = {
    AgentCreated,
    StatusChanged,
    ComplexityEvaluated,
    SubtasksDefined,
    CodeGenerationStarted,
    WorkCompleted,
    WorkFailed,
}
PASSED_EVENTS = {ChildSpawned, ChildCompleted}
THINKING_EVENTS = {ThoughtCaptured}


def event_to_schema(event: DomainEvent) -> EventSchema:
    """Convert a domain event to API schema."""
    # Get event-specific data (exclude base fields)
    data = event.model_dump(exclude={"aggregate_id", "sequence_number", "occurred_at"})

    return EventSchema(
        event_type=type(event).__name__,
        aggregate_id=str(event.aggregate_id),
        sequence_number=event.sequence_number,
        occurred_at=event.occurred_at,
        data=data,
    )


def categorize_event(event: DomainEvent) -> str:
    """Determine the category of an event."""
    event_type = type(event)
    if event_type in RECEIVED_EVENTS:
        return "received"
    if event_type in PRODUCED_EVENTS:
        return "produced"
    if event_type in PASSED_EVENTS:
        return "passed"
    if event_type in THINKING_EVENTS:
        return "thinking"
    return "produced"  # Default to produced


class IncrementalHierarchyTracker:
    """Tracks hierarchy state for incremental event fetching.

    Instead of fetching all events on every poll, this tracks:
    - Known agents in the hierarchy
    - Last seen sequence number per agent
    - Pending child IDs to explore (from ChildSpawned events)

    This reduces database load from O(total_events) to O(new_events) per poll.
    """

    def __init__(self, event_store: EventStoreReadPort, root_id: UUID) -> None:
        self._event_store = event_store
        self._root_id = root_id
        # Track last seen sequence per agent
        self._last_sequence: dict[UUID, int] = {}
        # Agents we know about
        self._known_agents: set[UUID] = set()
        # Child IDs discovered but not yet fetched
        self._pending_children: set[UUID] = set()
        # All events accumulated (for summary projection)
        self._all_events: list[DomainEvent] = []

    async def fetch_new_events(self) -> list[DomainEvent]:
        """Fetch only new events since last poll.

        Returns events in chronological order.
        """
        new_events: list[DomainEvent] = []

        # Initialize with root on first call
        if not self._known_agents:
            self._known_agents.add(self._root_id)

        # Process pending children (newly discovered agents)
        agents_to_check = list(self._known_agents) + list(self._pending_children)
        self._known_agents.update(self._pending_children)
        self._pending_children.clear()

        # Fetch new events from all known agents
        for agent_id in agents_to_check:
            last_seq = self._last_sequence.get(agent_id)

            # Fetch only events after last seen sequence
            events = await self._event_store.get_events(
                agent_id,
                after_sequence=last_seq,
            )

            if events:
                # Update last sequence for this agent
                self._last_sequence[agent_id] = events[-1].sequence_number
                new_events.extend(events)

                # Check for new children to explore
                for event in events:
                    if isinstance(event, ChildSpawned):
                        child_id = event.child_id
                        if child_id not in self._known_agents:
                            self._pending_children.add(child_id)

        # Accumulate for summary projection
        self._all_events.extend(new_events)

        # Sort new events by timestamp
        return sorted(new_events, key=lambda e: (e.occurred_at, e.sequence_number))

    def get_all_events(self) -> list[DomainEvent]:
        """Get all accumulated events (for summary projection)."""
        return sorted(self._all_events, key=lambda e: (e.occurred_at, e.sequence_number))

    def has_pending_children(self) -> bool:
        """Check if there are pending children to fetch."""
        return len(self._pending_children) > 0


@router.get("/{agent_id}", response_model=CategorizedEventsSchema)
async def get_agent_events(agent_id: UUID, event_store: EventStoreDep) -> CategorizedEventsSchema:
    """Get all events for an agent, categorized by type.

    Categories:
    - received: Events received by the agent (TaskAssigned)
    - produced: Events the agent produced (StatusChanged, ComplexityEvaluated, etc.)
    - passed: Events involving parent/child communication (ChildSpawned, ChildCompleted)
    - thinking: Agent's internal reasoning (ThoughtCaptured)
    """
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    result = CategorizedEventsSchema()

    for event in events:
        schema = event_to_schema(event)
        category = categorize_event(event)

        if category == "received":
            result.received.append(schema)
        elif category == "produced":
            result.produced.append(schema)
        elif category == "passed":
            result.passed.append(schema)
        elif category == "thinking":
            result.thinking.append(schema)

    return result


@router.get("/{agent_id}/all", response_model=list[EventSchema])
async def get_all_agent_events(agent_id: UUID, event_store: EventStoreDep) -> list[EventSchema]:
    """Get all events for an agent in chronological order."""
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return [event_to_schema(e) for e in events]


@router.get("/hierarchy/{root_id}/all", response_model=list[EventSchema])
async def get_hierarchy_events(root_id: UUID, event_store: EventStoreDep) -> list[EventSchema]:
    """Get all events for entire hierarchy (root + all descendants).

    Events are returned in chronological order.
    """
    collector = HierarchyCollector(event_store)
    all_events = await collector.collect(root_id)

    if not all_events:
        raise HTTPException(status_code=404, detail=f"Agent {root_id} not found")

    return [event_to_schema(e) for e in all_events]


@router.get("/sse/{root_id}")
async def sse_events(root_id: UUID, event_store: EventStoreDep) -> EventSourceResponse:
    """Server-Sent Events stream for real-time event updates.

    Uses incremental fetching to reduce database load:
    - Tracks last_sequence per agent
    - Only fetches events after last seen sequence
    - Discovers new child agents via ChildSpawned events

    Example client usage:
        const source = new EventSource('/api/events/sse/{root_id}');
        source.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log(data);
        };
    """

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        """Generate SSE events by incrementally polling for new events."""
        tracker = IncrementalHierarchyTracker(event_store, root_id)

        while True:
            try:
                # Fetch only new events (incremental)
                new_events = await tracker.fetch_new_events()

                # Keep fetching if new children were discovered
                while tracker.has_pending_children():
                    child_events = await tracker.fetch_new_events()
                    new_events.extend(child_events)

                # Stream new events to client
                for event in new_events:
                    schema = event_to_schema(event)
                    yield {
                        "event": "message",
                        "data": schema.model_dump_json(),
                    }

                # Poll interval
                await asyncio.sleep(0.5)

            except Exception as e:
                # Log full error details server-side
                logger.exception("SSE stream error for root_id=%s", root_id)
                # Return sanitized error to client
                yield {
                    "event": "error",
                    "data": '{"message": "Stream error occurred", "retry": true}',
                }
                break

    return EventSourceResponse(event_generator())


@router.get("/sse/{root_id}/summary")
async def sse_summary(root_id: UUID, event_store: EventStoreDep) -> EventSourceResponse:
    """Server-Sent Events stream for real-time execution summary updates.

    Uses incremental fetching to reduce database load:
    - Only fetches new events since last poll
    - Accumulates events for summary projection
    - Emits update only when new events are detected

    Example client usage:
        const source = new EventSource('/api/events/sse/{root_id}/summary');
        source.addEventListener('summary', (event) => {
            const summary = JSON.parse(event.data);
            console.log('Updated cost:', summary.cost.total_cost_usd);
        });
    """

    async def summary_generator() -> AsyncGenerator[dict[str, str], None]:
        """Generate SSE events with summary updates."""
        tracker = IncrementalHierarchyTracker(event_store, root_id)
        projection = SummaryProjection()
        last_event_count = 0

        while True:
            try:
                # Fetch only new events (incremental)
                new_events = await tracker.fetch_new_events()

                # Keep fetching if new children were discovered
                while tracker.has_pending_children():
                    await tracker.fetch_new_events()

                # Get all accumulated events
                all_events = tracker.get_all_events()
                current_count = len(all_events)

                # Only emit update if events changed
                if current_count != last_event_count:
                    last_event_count = current_count
                    summary = projection.project(all_events)
                    schema = _projection_summary_to_schema(summary)
                    yield {
                        "event": "summary",
                        "data": schema.model_dump_json(),
                    }

                # Poll interval
                await asyncio.sleep(0.5)

            except Exception as e:
                # Log full error details server-side
                logger.exception("SSE summary stream error for root_id=%s", root_id)
                # Return sanitized error to client
                yield {
                    "event": "error",
                    "data": '{"message": "Summary stream error occurred", "retry": true}',
                }
                break

    return EventSourceResponse(summary_generator())


@router.get("/{agent_id}/prompts", response_model=AgentPromptsSchema)
async def get_agent_prompts(agent_id: UUID, event_store: EventStoreDep) -> AgentPromptsSchema:
    """Get all prompts sent by an agent.

    Returns prompts for complexity evaluation, task decomposition, and worker execution.
    """
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    prompts = []
    for event in events:
        if isinstance(event, PromptSent):
            prompts.append(
                AgentPromptSchema(
                    prompt=event.prompt,
                    prompt_type=event.prompt_type,
                    target=event.target,
                    occurred_at=event.occurred_at,
                )
            )

    return AgentPromptsSchema(
        agent_id=str(agent_id),
        prompts=prompts,
        total=len(prompts),
    )
