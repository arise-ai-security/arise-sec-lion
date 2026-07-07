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

from core.application.services import EventBroadcaster
from core.domain.events.events import PromptSent
from core.query.projections.hierarchy_collector import HierarchyCollector
from core.query.projections.impl.summary import IncrementalSummaryProjection
from query.api.dependencies import EventStoreDep
from query.api.event_mapping import categorize_event, event_to_schema
from query.api.routes.agents import _projection_summary_to_schema
from query.api.schemas import (
    AgentPromptSchema,
    AgentPromptsSchema,
    CategorizedEventsSchema,
    PaginatedEventsSchema,
    PaginationMetaSchema,
)
from query.api.streaming import IncrementalHierarchyTracker


logger = logging.getLogger(__name__)


router = APIRouter()


@router.get("/{agent_id}", response_model=CategorizedEventsSchema)
async def get_agent_events(agent_id: UUID, event_store: EventStoreDep) -> CategorizedEventsSchema:
    """Get all events for an agent, categorized by type.

    Categories:
    - received: Events received by the agent (TaskAssigned)
    - produced: Events the agent produced (StatusChanged, ComplexityEvaluated, etc.)
    - passed: Parent/child communication (ChildSpawned, ChildCompleted, ChildFailed)
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

            except Exception:
                # Log full error details server-side
                logger.exception("SSE summary stream error for root_id=%s", root_id)
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
