"""Event-related API routes.

Provides endpoints for querying events and SSE streaming.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from core.domain.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.query.projections.hierarchy_collector import HierarchyCollector
from core.query.projections.impl import SummaryProjection
from query.api.dependencies import EventStoreDep
from query.api.routes.agents import _projection_summary_to_schema
from query.api.schemas import CategorizedEventsSchema, EventSchema, ExecutionSummarySchema


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

    Polls the event store and streams new events as they occur.
    Client receives events in JSON format.

    Example client usage:
        const source = new EventSource('/api/events/sse/{root_id}');
        source.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log(data);
        };
    """

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        """Generate SSE events by polling for new events."""
        collector = HierarchyCollector(event_store)
        seen_events: set[tuple[str, int]] = set()

        while True:
            try:
                # Collect all events in hierarchy
                all_events = await collector.collect(root_id)

                for event in all_events:
                    event_key = (str(event.aggregate_id), event.sequence_number)
                    if event_key not in seen_events:
                        seen_events.add(event_key)
                        schema = event_to_schema(event)
                        yield {
                            "event": "message",
                            "data": schema.model_dump_json(),
                        }

                # Send ping to keep connection alive and force flush
                # This ensures browsers/proxies don't buffer the response
                yield {"event": "ping", "data": ""}

                # Poll interval - shorter for more responsive updates
                await asyncio.sleep(0.3)

            except Exception as e:
                yield {
                    "event": "error",
                    "data": str(e),
                }
                break

    return EventSourceResponse(event_generator())


@router.get("/sse/{root_id}/summary")
async def sse_summary(root_id: UUID, event_store: EventStoreDep) -> EventSourceResponse:
    """Server-Sent Events stream for real-time execution summary updates.

    This endpoint provides real-time updates of the execution summary
    (costs, timing, node counts) as the agent hierarchy executes.

    Updates are sent whenever new events are detected. The client receives
    a complete ExecutionSummarySchema on each update.

    Example client usage:
        const source = new EventSource('/api/events/sse/{root_id}/summary');
        source.addEventListener('summary', (event) => {
            const summary = JSON.parse(event.data);
            console.log('Updated cost:', summary.cost.total_cost_usd);
        });
    """

    async def summary_generator() -> AsyncGenerator[dict[str, str], None]:
        """Generate SSE events with summary updates."""
        collector = HierarchyCollector(event_store)
        projection = SummaryProjection()
        last_event_count = 0

        while True:
            try:
                # Collect all events in hierarchy
                all_events = await collector.collect(root_id)
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
                yield {
                    "event": "error",
                    "data": str(e),
                }
                break

    return EventSourceResponse(summary_generator())
