"""Agent-related API routes.

Provides endpoints for querying agent hierarchy and individual agents.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from core.application.projections.hierarchy_collector import HierarchyCollector
from core.domain.events import AgentCreated
from core.domain.model import AgentSession
from presentation.api.dependencies import EventStoreDep
from presentation.api.schemas import AgentHierarchySchema, AgentListItemSchema, AgentNodeSchema


router = APIRouter()


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

    # Build agent lookup
    agents_by_id: dict[UUID, AgentSession] = {}
    for aid in all_agent_ids:
        events = await event_store.get_events(aid)
        if events:
            agents_by_id[aid] = AgentSession.load_from_history(events)

    # Build tree recursively
    def build_node(aid: UUID) -> AgentNodeSchema:
        agent = agents_by_id[aid]
        children = [build_node(cid) for cid in agent.child_ids if cid in agents_by_id]
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
