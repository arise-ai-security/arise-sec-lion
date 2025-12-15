"""Agent-related API routes.

Provides endpoints for querying agent hierarchy and individual agents.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from core.domain.events import (
    AgentCreated,
    BudgetAllocated,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    SubtasksDefined,
)
from core.domain.model import AgentSession
from core.query.projections.hierarchy_collector import HierarchyCollector
from query.api.dependencies import EventStoreDep
from query.api.schemas import (
    AgentHierarchySchema,
    AgentListItemSchema,
    AgentNodeSchema,
    AgentSummarySchema,
    BudgetInfoSchema,
    SubtaskSummarySchema,
    TaskQueueItemSchema,
)


router = APIRouter()


def _build_config_details(config: object, strategy: str | None) -> dict:
    """Build config details dict based on strategy type."""
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


@router.get("", response_model=list[AgentListItemSchema])
async def list_boss_agents(event_store: EventStoreDep) -> list[AgentListItemSchema]:
    """List all BOSS (root) agents.

    Returns only top-level agents that have no parent.
    These represent individual task runs.
    """
    all_ids = await event_store.get_all_aggregate_ids()
    boss_agents = []

    for agent_id in all_ids:
        events = await event_store.get_events(agent_id)
        if not events:
            continue

        agent = AgentSession.load_from_history(events)

        # Only include BOSS agents (no parent)
        if agent.parent_id is None:
            # Find creation time from AgentCreated event
            created_at = None
            for event in events:
                if isinstance(event, AgentCreated):
                    created_at = event.occurred_at
                    break

            boss_agents.append(
                AgentListItemSchema(
                    id=str(agent.session_id),
                    role=agent.role.value,
                    status=agent.status.value,
                    task_description=agent.task_description or "",
                    created_at=created_at,
                )
            )

    # Sort by creation time (newest first)
    boss_agents.sort(key=lambda a: a.created_at or "", reverse=True)
    return boss_agents


@router.get("/{agent_id}", response_model=AgentListItemSchema)
async def get_agent(agent_id: UUID, event_store: EventStoreDep) -> AgentListItemSchema:
    """Get a single agent by ID."""
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    agent = AgentSession.load_from_history(events)

    # Find creation time
    created_at = None
    for event in events:
        if isinstance(event, AgentCreated):
            created_at = event.occurred_at
            break

    return AgentListItemSchema(
        id=str(agent.session_id),
        role=agent.role.value,
        status=agent.status.value,
        task_description=agent.task_description or "",
        created_at=created_at,
    )


@router.get("/{agent_id}/hierarchy", response_model=AgentHierarchySchema)
async def get_agent_hierarchy(agent_id: UUID, event_store: EventStoreDep) -> AgentHierarchySchema:
    """Get the complete hierarchy tree for an agent.

    Args:
        agent_id: Root agent UUID (typically BOSS).

    Returns:
        Complete hierarchy tree with all descendants.
    """
    # Verify root agent exists
    root_events = await event_store.get_events(agent_id)
    if not root_events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    # Use HierarchyCollector to traverse the tree
    collector = HierarchyCollector(event_store)
    all_agent_ids = await collector.collect_agent_ids(agent_id)
    depth = await collector.get_hierarchy_depth(agent_id)

    # Build agent lookup - include agents even if their events haven't been created yet
    # This handles the race condition where ChildSpawned is persisted on parent
    # but the child's AgentCreated hasn't been persisted yet
    agents_by_id: dict[UUID, AgentSession] = {}
    pending_agent_ids: set[UUID] = set()  # Track IDs that exist but have no events yet

    for aid in all_agent_ids:
        events = await event_store.get_events(aid)
        if events:
            agents_by_id[aid] = AgentSession.load_from_history(events)
        else:
            # Child was spawned but its events haven't been created yet
            pending_agent_ids.add(aid)

    # Build tree recursively
    def build_node(aid: UUID) -> AgentNodeSchema:
        if aid in pending_agent_ids:
            # Return a placeholder node for agents whose events haven't been created yet
            return AgentNodeSchema(
                id=str(aid),
                role="pending",
                status="pending",
                task_description="(initializing...)",
                parent_id=None,
                children=[],
            )

        agent = agents_by_id[aid]
        # Include children that are either fully loaded OR pending
        children = [
            build_node(cid)
            for cid in agent.child_ids
            if cid in agents_by_id or cid in pending_agent_ids
        ]
        return AgentNodeSchema(
            id=str(aid),
            role=agent.role.value,
            status=agent.status.value,
            task_description=agent.task_description or "",
            parent_id=str(agent.parent_id) if agent.parent_id else None,
            children=children,
        )

    root_node = build_node(agent_id)

    return AgentHierarchySchema(
        root=root_node,
        total_agents=len(all_agent_ids),
        depth=depth,
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
    events = await event_store.get_events(agent_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    agent = AgentSession.load_from_history(events)

    # Extract data from events for the projection
    complexity: str | None = None
    complexity_reasoning: str | None = None
    worker_tool: str | None = None
    subtasks_list: list[SubtaskSummarySchema] = []
    child_id_map: dict[int, UUID] = {}  # Map subtask index to child_id

    # Budget tracking
    initial_budget: float = 0.0
    budget_source: str | None = None

    for event in events:
        if isinstance(event, ComplexityEvaluated):
            complexity = event.complexity
            complexity_reasoning = event.reasoning

        elif isinstance(event, CodeGenerationStarted):
            worker_tool = event.tool_name

        elif isinstance(event, BudgetAllocated):
            initial_budget = event.amount
            budget_source = event.source

        elif isinstance(event, SubtasksDefined):
            # Store subtasks - we'll match children later
            for subtask in event.subtasks:
                subtasks_list.append(
                    SubtaskSummarySchema(
                        description=subtask.description,
                        child_id=None,
                        child_status=None,
                    )
                )

        elif isinstance(event, ChildSpawned):
            # Match child to subtask by index (children spawned in subtask order)
            child_idx = len(child_id_map)
            child_id_map[child_idx] = event.child_id

    # Update subtasks with child IDs and statuses
    for idx, child_id in child_id_map.items():
        if idx < len(subtasks_list):
            child_events = await event_store.get_events(child_id)
            if child_events:
                child_agent = AgentSession.load_from_history(child_events)
                subtasks_list[idx] = SubtaskSummarySchema(
                    description=subtasks_list[idx].description,
                    child_id=str(child_id),
                    child_status=child_agent.status.value,
                )

    # Extract config details
    config_strategy: str | None = None
    config_details: dict = {}

    if hasattr(agent, "config") and agent.config:
        config = agent.config
        config_strategy = getattr(config, "strategy", None)
        config_details = _build_config_details(config, config_strategy)

    # Build budget info
    budget_info: BudgetInfoSchema | None = None
    if initial_budget > 0 or agent.current_budget > 0:
        budget_info = BudgetInfoSchema(
            current_budget=agent.current_budget,
            initial_budget=initial_budget,
            spent=initial_budget - agent.current_budget,
            source=budget_source,
        )

    # Build task queue
    task_queue_items = [
        TaskQueueItemSchema(description=task.description, priority=0)
        for task in agent.task_queue
    ]

    return AgentSummarySchema(
        id=str(agent.session_id),
        role=agent.role.value,
        status=agent.status.value,
        task_description=agent.task_description or "",
        complexity=complexity,
        complexity_reasoning=complexity_reasoning,
        worker_tool=worker_tool,
        subtasks=subtasks_list,
        config_strategy=config_strategy,
        config_details=config_details,
        result=agent.result,
        error_message=agent.error_message,
        budget=budget_info,
        task_queue=task_queue_items,
        queue_size=len(task_queue_items),
    )
