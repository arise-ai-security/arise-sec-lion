"""Event-to-DTO mapping and categorization for the query API.

Maps domain events to API schema DTOs and categorizes them into
received/produced/passed/thinking buckets for the events endpoints.
"""

from core.domain.events.events import (
    AgentCreated,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DecisionInfeasible,
    DomainEvent,
    ProbeCompleted,
    ProbeStarted,
    RedecompositionTriggered,
    RetryScheduled,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    VerificationFailed,
    VerificationPassed,
    WorkCompleted,
    WorkFailed,
)
from query.api.schemas import EventSchema


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
    VerificationFailed,
    VerificationPassed,
    DecisionInfeasible,
    RetryScheduled,
    RedecompositionTriggered,
    ProbeStarted,
    ProbeCompleted,
}
PASSED_EVENTS = {ChildSpawned, ChildCompleted, ChildFailed}
THINKING_EVENTS = {ThoughtCaptured}


def event_to_schema(event: DomainEvent) -> EventSchema:
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
