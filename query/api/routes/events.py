"""Event-related API routes.

Provides endpoints for querying events and SSE streaming.
SSE endpoints use incremental fetching to reduce database load.

Real-time streaming of ThoughtCaptured events is enabled via EventBroadcaster.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from core.application.services.event_broadcaster import EventBroadcaster

logger = logging.getLogger(__name__)

from core.domain.events.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    PromptSent,
    ResearchCompleted,
    ResearchStarted,
    ResearchToolCalled,
    RoleTransitioned,
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
from core.query.projections.impl.summary import IncrementalSummaryProjection
from query.api.dependencies import EventStoreDep
from query.api.routes.agents import _projection_summary_to_schema
from query.api.schemas import (
    AgentPromptSchema,
    AgentPromptsSchema,
    CategorizedEventsSchema,
    EventSchema,
    PaginatedEventsSchema,
    PaginationMetaSchema,
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
    ResearchStarted,
    ResearchToolCalled,
    ResearchCompleted,
    RoleTransitioned,
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

    Uses optimized batch fetching on first load, then batch incremental updates.
    This reduces database load from O(total_events) to O(new_events) per poll.

    Optimized: Uses single batch query for incremental updates instead of N queries.
    """

    def __init__(self, event_store: EventStoreReadPort, root_id: UUID) -> None:
        self._event_store = event_store
        self._root_id = root_id
        # Track last seen sequence per agent
        self._last_sequence: dict[UUID, int] = {}
        # Agents we know about
        self._known_agents: set[UUID] = set()
        # All events accumulated (for summary projection)
        self._all_events: list[DomainEvent] = []
        # Whether initial load has been done
        self._initialized = False
        # New children discovered that need initial fetch
        self._pending_children: set[UUID] = set()

    async def fetch_new_events(self) -> list[DomainEvent]:
        """Fetch only new events since last poll.

        First call uses optimized single-query approach for entire hierarchy.
        Subsequent calls use batch incremental fetch (1 query instead of N).
        """
        new_events: list[DomainEvent] = []

        # First call: use optimized single-query approach
        if not self._initialized:
            self._initialized = True
            grouped = await self._event_store.get_hierarchy_events_grouped(self._root_id)

            for agent_id, events in grouped.items():
                self._known_agents.add(agent_id)
                if events:
                    self._last_sequence[agent_id] = events[-1].sequence_number
                    new_events.extend(events)

            self._all_events.extend(new_events)
            return sorted(new_events, key=lambda e: (e.occurred_at, e.sequence_number))

        # Subsequent calls: batch incremental fetch (1 query instead of N)
        # Build dict of agent_id -> last_sequence for batch query
        agent_sequences: dict[UUID, int | None] = {}
        for agent_id in self._known_agents:
            agent_sequences[agent_id] = self._last_sequence.get(agent_id)

        # Include pending children (new agents discovered via ChildSpawned)
        for child_id in self._pending_children:
            agent_sequences[child_id] = None  # Fetch all events for new agents
        self._pending_children.clear()

        # Single batch query for all agents
        if agent_sequences:
            grouped = await self._event_store.get_events_batch_incremental(agent_sequences)

            for agent_id, events in grouped.items():
                if events:
                    self._last_sequence[agent_id] = events[-1].sequence_number
                    new_events.extend(events)

                    # Discover new children
                    for event in events:
                        if isinstance(event, ChildSpawned):
                            child_id = event.child_id
                            if child_id not in self._known_agents:
                                self._known_agents.add(child_id)
                                self._pending_children.add(child_id)

        self._all_events.extend(new_events)
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


# Pagination constants
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 1000


@router.get("/{agent_id}/all", response_model=PaginatedEventsSchema)
async def get_all_agent_events(
    agent_id: UUID,
    event_store: EventStoreDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_EVENT_LIMIT, description="Maximum number of events to return"),
    ] = DEFAULT_EVENT_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of events to skip"),
    ] = 0,
) -> PaginatedEventsSchema:
    """Get all events for an agent in chronological order with pagination."""
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    total = len(events)
    # Apply pagination
    paginated_events = events[offset : offset + limit + 1]
    has_more = len(paginated_events) > limit
    if has_more:
        paginated_events = paginated_events[:limit]

    return PaginatedEventsSchema(
        items=[event_to_schema(e) for e in paginated_events],
        pagination=PaginationMetaSchema(
            limit=limit,
            offset=offset,
            total=total,
            has_more=has_more,
        ),
    )


@router.get("/hierarchy/{root_id}/all", response_model=PaginatedEventsSchema)
async def get_hierarchy_events(
    root_id: UUID,
    event_store: EventStoreDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_EVENT_LIMIT, description="Maximum number of events to return"),
    ] = DEFAULT_EVENT_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of events to skip"),
    ] = 0,
) -> PaginatedEventsSchema:
    """Get all events for entire hierarchy (root + all descendants) with pagination.

    Events are returned in chronological order.
    """
    collector = HierarchyCollector(event_store)
    all_events = await collector.collect(root_id)

    if not all_events:
        raise HTTPException(status_code=404, detail=f"Agent {root_id} not found")

    total = len(all_events)
    # Apply pagination
    paginated_events = all_events[offset : offset + limit + 1]
    has_more = len(paginated_events) > limit
    if has_more:
        paginated_events = paginated_events[:limit]

    return PaginatedEventsSchema(
        items=[event_to_schema(e) for e in paginated_events],
        pagination=PaginationMetaSchema(
            limit=limit,
            offset=offset,
            total=total,
            has_more=has_more,
        ),
    )


@router.get("/sse/{root_id}")
async def sse_events(root_id: UUID, event_store: EventStoreDep) -> EventSourceResponse:
    """Server-Sent Events stream for real-time event updates.

    Uses hybrid approach for optimal real-time streaming:
    - Real-time push via EventBroadcaster for ThoughtCaptured events (no delay)
    - Incremental polling for other events (500ms interval)

    Real-time events use 'thought' event type, polled events use 'message'.

    Example client usage:
        const source = new EventSource('/api/events/sse/{root_id}');
        source.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log(data);
        };
        source.addEventListener('thought', (event) => {
            const data = JSON.parse(event.data);
            // Real-time ThoughtCaptured event
        });
    """

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        """Generate SSE events with real-time and polled events."""
        tracker = IncrementalHierarchyTracker(event_store, root_id)
        broadcaster = EventBroadcaster.get_instance()

        # Track real-time event IDs to avoid duplicates from polling
        seen_realtime_ids: set[tuple[str, int]] = set()

        async with broadcaster.subscribe(root_id) as realtime_queue:
            while True:
                try:
                    # 1. Drain real-time queue (non-blocking) for immediate ThoughtCaptured events
                    while True:
                        try:
                            event = realtime_queue.get_nowait()
                            # Track to avoid duplicates when polling later
                            event_key = (str(event.aggregate_id), event.sequence_number)
                            seen_realtime_ids.add(event_key)
                            schema = event_to_schema(event)
                            yield {
                                "event": "thought",
                                "data": schema.model_dump_json(),
                            }
                        except asyncio.QueueEmpty:
                            break

                    # 2. Fetch events from database (incremental)
                    new_events = await tracker.fetch_new_events()

                    # Keep fetching if new children were discovered
                    while tracker.has_pending_children():
                        child_events = await tracker.fetch_new_events()
                        new_events.extend(child_events)

                    # 3. Stream polled events to client (skip duplicates from real-time)
                    for event in new_events:
                        event_key = (str(event.aggregate_id), event.sequence_number)
                        if event_key in seen_realtime_ids:
                            # Skip - already sent via real-time
                            continue

                        schema = event_to_schema(event)
                        yield {
                            "event": "message",
                            "data": schema.model_dump_json(),
                        }

                    # Limit memory growth of seen_realtime_ids (keep last 10000)
                    if len(seen_realtime_ids) > 10000:
                        # Convert to list, sort by sequence, keep recent
                        seen_list = list(seen_realtime_ids)
                        seen_realtime_ids = set(seen_list[-5000:])

                    # Poll interval
                    await asyncio.sleep(0.5)

                except Exception:
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
        # Use incremental projection: O(new_events) instead of O(all_events)
        projection = IncrementalSummaryProjection()

        while True:
            try:
                # Fetch only new events (incremental)
                new_events = await tracker.fetch_new_events()

                # Keep fetching if new children were discovered
                while tracker.has_pending_children():
                    child_events = await tracker.fetch_new_events()
                    new_events.extend(child_events)

                # Only emit update if there are new events
                if new_events:
                    # Incremental update: only process new events
                    summary = projection.update(new_events)
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
