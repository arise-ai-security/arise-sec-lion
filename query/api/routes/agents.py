"""Agent-related API routes.

Provides endpoints for querying agent hierarchy and individual agents.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from core.domain.model import AgentRole
from core.query.projections.hierarchy_builder import AgentNode, HierarchyBuilder
from core.query.projections.hierarchy_collector import HierarchyCollector
from core.query.projections.impl import AgentListProjection, AgentSummaryService, SummaryProjection
from core.query.projections.models import AgentListItem, AgentSummary, ProjectionSummary
from query.api.dependencies import EventStoreDep
from query.api.schemas import (
    AgentHierarchySchema,
    AgentListItemSchema,
    AgentNodeSchema,
    AgentSummarySchema,
    CostBreakdownSchema,
    ExecutionSummarySchema,
    ExecutionTimingSchema,
    PaginatedAgentListSchema,
    PaginationMetaSchema,
    RoleCostBreakdownSchema,
    RoleCountSchema,
    RoleTokensSchema,
    SubtaskSummarySchema,
)


router = APIRouter()


def _agent_list_item_to_schema(item: AgentListItem) -> AgentListItemSchema:
    """Convert AgentListItem read model to API schema."""
    return AgentListItemSchema(
        id=str(item.agent_id),
        role=item.role,
        status=item.status,
        task_description=item.task_description or "",
        created_at=item.created_at,
    )


def _agent_node_to_schema(node: AgentNode) -> AgentNodeSchema:
    """Convert AgentNode to API schema recursively."""
    return AgentNodeSchema(
        id=str(node.id),
        role=node.role,
        status=node.status,
        task_description=node.task_description,
        parent_id=str(node.parent_id) if node.parent_id else None,
        children=[_agent_node_to_schema(child) for child in node.children],
    )


def _agent_summary_to_schema(summary: AgentSummary) -> AgentSummarySchema:
    """Convert AgentSummary read model to API schema."""
    return AgentSummarySchema(
        id=str(summary.agent_id),
        role=summary.role,
        status=summary.status,
        task_description=summary.task_description,
        complexity=summary.complexity,
        complexity_reasoning=summary.complexity_reasoning,
        worker_tool=summary.worker_tool,
        subtasks=[
            SubtaskSummarySchema(
                description=s.description,
                child_id=str(s.child_id) if s.child_id else None,
                child_status=s.child_status,
            )
            for s in summary.subtasks
        ],
        config_strategy=summary.config_strategy,
        config_details=summary.config_details,
        result=summary.result,
        error_message=summary.error_message,
    )


# Pagination constants
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


@router.get("", response_model=PaginatedAgentListSchema)
async def list_boss_agents(
    event_store: EventStoreDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Maximum number of agents to return"),
    ] = DEFAULT_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of agents to skip"),
    ] = 0,
) -> PaginatedAgentListSchema:
    """List all BOSS (root) agents with pagination.

    Returns only top-level agents that have no parent.
    These represent individual task runs.

    Args:
        limit: Maximum number of agents to return (1-1000, default 100).
        offset: Number of agents to skip for pagination (default 0).

    Returns:
        Paginated list of BOSS agents with pagination metadata.

    Uses optimized single-query approach that filters BOSS agents at database level.
    """
    # Optimized: filter BOSS agents at database level (single round trip)
    boss_events_grouped = await event_store.get_boss_agents_grouped(
        limit=limit + 1,  # Fetch one extra to check has_more
        offset=offset,
    )

    # Use projection to build lightweight read models (no AgentSession reconstruction)
    projection = AgentListProjection()
    boss_agents = list(projection.project_all(boss_events_grouped).values())

    # Check if there are more results
    has_more = len(boss_agents) > limit
    if has_more:
        boss_agents = boss_agents[:limit]

    # Convert to API schemas
    items = [_agent_list_item_to_schema(agent) for agent in boss_agents]

    return PaginatedAgentListSchema(
        items=items,
        pagination=PaginationMetaSchema(
            limit=limit,
            offset=offset,
            total=offset + len(items) + (1 if has_more else 0),  # Approximate total
            has_more=has_more,
        ),
    )


@router.get("/{agent_id}", response_model=AgentListItemSchema)
async def get_agent(agent_id: UUID, event_store: EventStoreDep) -> AgentListItemSchema:
    """Get a single agent by ID.

    Uses CQRS projection for efficient read model construction.
    """
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    # Use projection to build lightweight read model
    projection = AgentListProjection()
    agent_item = projection.project(events)

    if agent_item is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return _agent_list_item_to_schema(agent_item)


@router.get("/{agent_id}/hierarchy", response_model=AgentHierarchySchema)
async def get_agent_hierarchy(agent_id: UUID, event_store: EventStoreDep) -> AgentHierarchySchema:
    """Get the complete hierarchy tree for an agent.

    Args:
        agent_id: Root agent UUID (typically BOSS).

    Returns:
        Complete hierarchy tree with all descendants.

    Uses optimized recursive CTE query to fetch only hierarchy events.
    """
    # Optimized: fetch only events for this hierarchy using recursive CTE
    hierarchy_events = await event_store.get_hierarchy_events_grouped(agent_id)

    # Check root exists
    if not hierarchy_events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    # Use projection to build lightweight read models
    projection = AgentListProjection()
    agents_by_id = projection.project_all(hierarchy_events)

    # Build hierarchy tree using dedicated service
    builder = HierarchyBuilder(agents_by_id)
    try:
        hierarchy = builder.build(agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return AgentHierarchySchema(
        root=_agent_node_to_schema(hierarchy.root),
        total_agents=hierarchy.total_agents,
        depth=hierarchy.depth,
    )


@router.get("/{agent_id}/summary", response_model=AgentSummarySchema)
async def get_agent_summary(agent_id: UUID, event_store: EventStoreDep) -> AgentSummarySchema:
    """Get a summary projection for an agent.

    This CQRS projection aggregates data from multiple events to provide:
    - Task description and status
    - Complexity evaluation reasoning (why WORKER vs MANAGER)
    - For WORKERs: which tool is being used
    - For MANAGERs: subtasks and their child agents
    - Configuration details (hyperparameters)

    Args:
        agent_id: Agent UUID to get summary for.

    Returns:
        AgentSummarySchema with aggregated agent information.
    """
    service = AgentSummaryService(event_store)
    summary = await service.build(agent_id)

    if summary is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return _agent_summary_to_schema(summary)


def _projection_summary_to_schema(summary: ProjectionSummary) -> ExecutionSummarySchema:
    """Convert ProjectionSummary to ExecutionSummarySchema.

    Args:
        summary: The projection summary with costs, timing, and node counts.

    Returns:
        ExecutionSummarySchema for API response.
    """
    # Convert cost breakdown
    cost = summary.cost
    cost_schema = CostBreakdownSchema(
        total_cost_usd=cost.total_cost_usd if cost else 0.0,
        llm_cost_usd=cost.llm_cost_usd if cost else 0.0,
        worker_cost_usd=cost.worker_cost_usd if cost else 0.0,
        total_tokens=cost.total_tokens if cost else 0,
        prompt_tokens=cost.prompt_tokens if cost else 0,
        completion_tokens=cost.completion_tokens if cost else 0,
        cost_by_role=RoleCostBreakdownSchema(
            BOSS=cost.cost_by_role.get(AgentRole.BOSS.name, 0.0) if cost else 0.0,
            MANAGER=cost.cost_by_role.get(AgentRole.MANAGER.name, 0.0) if cost else 0.0,
            WORKER=cost.cost_by_role.get(AgentRole.WORKER.name, 0.0) if cost else 0.0,
            PENDING=cost.cost_by_role.get(AgentRole.PENDING.name, 0.0) if cost else 0.0,
            UNKNOWN=cost.cost_by_role.get("UNKNOWN", 0.0) if cost else 0.0,
        ),
        cost_by_model=dict(cost.cost_by_model) if cost else {},
        cost_by_operation=dict(cost.cost_by_operation) if cost else {},
        cost_by_agent=dict(cost.cost_by_agent) if cost else {},
        tokens_by_role=RoleTokensSchema(
            BOSS=cost.tokens_by_role.get(AgentRole.BOSS.name, 0) if cost else 0,
            MANAGER=cost.tokens_by_role.get(AgentRole.MANAGER.name, 0) if cost else 0,
            WORKER=cost.tokens_by_role.get(AgentRole.WORKER.name, 0) if cost else 0,
            PENDING=cost.tokens_by_role.get(AgentRole.PENDING.name, 0) if cost else 0,
        ),
        budget_limit_usd=cost.budget_limit_usd if cost else None,
        budget_remaining_usd=cost.budget_remaining_usd if cost else None,
        budget_exceeded=cost.budget_exceeded if cost else False,
    )

    # Convert node counts
    node_counts = summary.node_counts
    node_counts_schema = RoleCountSchema(
        BOSS=node_counts.by_role.get(AgentRole.BOSS.name, 0) if node_counts else 0,
        MANAGER=node_counts.by_role.get(AgentRole.MANAGER.name, 0) if node_counts else 0,
        WORKER=node_counts.by_role.get(AgentRole.WORKER.name, 0) if node_counts else 0,
        PENDING=node_counts.by_role.get(AgentRole.PENDING.name, 0) if node_counts else 0,
        total=node_counts.total if node_counts else 0,
    )

    # Convert timing
    timing = summary.execution_time
    timing_schema = ExecutionTimingSchema(
        total_seconds=timing.total_seconds if timing else 0.0,
        by_role=dict(timing.per_role) if timing else {},
        by_phase=dict(timing.per_phase) if timing else {},
        by_agent=dict(timing.per_agent) if timing else {},
    )

    # Determine if execution is complete (all agents in terminal state)
    # This is a simplification - actual check would need agent states
    is_complete = summary.error_count == 0 and summary.total_events > 0

    return ExecutionSummarySchema(
        total_events=summary.total_events,
        events_by_type=dict(summary.events_by_type),
        first_event=summary.first_event,
        last_event=summary.last_event,
        error_count=summary.error_count,
        node_counts=node_counts_schema,
        cost=cost_schema,
        timing=timing_schema,
        is_complete=is_complete,
    )


@router.get("/{agent_id}/execution-summary", response_model=ExecutionSummarySchema)
async def get_execution_summary(
    agent_id: UUID, event_store: EventStoreDep
) -> ExecutionSummarySchema:
    """Get comprehensive execution summary for an agent hierarchy.

    This endpoint aggregates cost, timing, and node count data from all events
    in the agent hierarchy (root + all descendants).

    The summary includes:
    - Event statistics (total, by type, errors)
    - Cost breakdown (total, by role, by model, by operation, by agent)
    - Token usage (total, by role)
    - Execution timing (total, by role, by phase, by agent)
    - Node counts (total, by role)
    - Budget tracking (if configured)

    Args:
        agent_id: Root agent UUID (typically BOSS).

    Returns:
        ExecutionSummarySchema with comprehensive execution metrics.
    """
    # Verify agent exists
    root_events = await event_store.get_events(agent_id)
    if not root_events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    # Collect all events in the hierarchy
    collector = HierarchyCollector(event_store)
    all_events = await collector.collect(agent_id)

    # Project into summary
    projection = SummaryProjection()
    summary = projection.project(all_events)

    return _projection_summary_to_schema(summary)
