"""Prompt trace API routes.

Provides endpoints for visualizing prompt provenance across the agent hierarchy.
Used by the Prompt Trace Viewer frontend.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from core.application.services.prompt_parser import PromptParser
from core.application.services.prompt_trace_service import PromptTraceService
from core.domain.values.prompt_trace import AgentNode, HierarchyTrace, ParsedPrompt
from query.api.dependencies import DomainPluginDep, EventStoreDep
from query.api.schemas import (
    HierarchyTraceSchema,
    ParsedPromptSchema,
    PromptSectionSchema,
    TraceAgentNodeSchema,
)

router = APIRouter()


def _prompt_to_schema(prompt: ParsedPrompt) -> ParsedPromptSchema:
    """Convert domain ParsedPrompt to API schema."""
    sections = [
        PromptSectionSchema(
            tag=s.tag,
            content=s.content,
            provenance=s.provenance.value,
        )
        for s in prompt.sections
    ]

    # Group sections by provenance
    sections_by_prov: dict[str, list[PromptSectionSchema]] = {}
    for section in sections:
        prov_key = section.provenance
        if prov_key not in sections_by_prov:
            sections_by_prov[prov_key] = []
        sections_by_prov[prov_key].append(section)

    return ParsedPromptSchema(
        raw=prompt.raw,
        raw_length=len(prompt.raw),
        occurred_at=prompt.occurred_at,
        prompt_type=prompt.prompt_type,
        target=prompt.target,
        sections=sections,
        sections_by_provenance=sections_by_prov,
    )


def _agent_node_to_schema(node: AgentNode) -> TraceAgentNodeSchema:
    """Convert domain AgentNode to API schema."""
    prompts = [_prompt_to_schema(p) for p in node.prompts]

    return TraceAgentNodeSchema(
        agent_id=str(node.agent_id),
        role=node.role,
        depth=node.depth,
        task=node.task,
        sibling_index=node.sibling_index,
        prompt_count=len(prompts),
        prompts=prompts,
        children=[_agent_node_to_schema(c) for c in node.children],
    )


def _find_agent_in_tree(node: AgentNode, target_id: UUID) -> AgentNode | None:
    """Recursively search for an agent in the tree."""
    if node.agent_id == target_id:
        return node
    for child in node.children:
        found = _find_agent_in_tree(child, target_id)
        if found:
            return found
    return None


@router.get("/trace/{root_id}", response_model=HierarchyTraceSchema)
async def get_hierarchy_trace(
    root_id: UUID,
    event_store: EventStoreDep,
    domain_plugin: DomainPluginDep,
) -> HierarchyTraceSchema:
    """Get the complete prompt trace for an agent hierarchy.

    Returns the full tree structure with all prompts parsed into
    provenance-tagged sections. Use this for the Prompt Trace Viewer UI.

    Args:
        root_id: UUID of the root agent (BOSS).
        event_store: Injected event store dependency.

    Returns:
        Complete hierarchy trace with parsed prompts.

    Raises:
        HTTPException: 404 if agent not found.
    """
    service = PromptTraceService(
        event_store,
        PromptParser(
            extra_tag_mappings=domain_plugin.get_tag_mappings() if domain_plugin else None,
            extra_provenance_patterns=(
                domain_plugin.get_provenance_patterns() if domain_plugin else None
            ),
        ),
    )
    trace = await service.trace(root_id)

    if trace.total_agents == 0:
        raise HTTPException(status_code=404, detail=f"Agent {root_id} not found")

    return HierarchyTraceSchema(
        root=_agent_node_to_schema(trace.root),
        total_agents=trace.total_agents,
        max_depth=trace.max_depth,
    )


@router.get("/agent/{agent_id}", response_model=TraceAgentNodeSchema)
async def get_agent_trace(
    agent_id: UUID,
    event_store: EventStoreDep,
    domain_plugin: DomainPluginDep,
) -> TraceAgentNodeSchema:
    """Get the prompt trace for a single agent.

    Fetches only the target agent's events (1 query) - useful for
    detail views without loading the full hierarchy.

    Args:
        agent_id: UUID of the agent to fetch.
        event_store: Injected event store dependency.

    Returns:
        Trace data for the specified agent.

    Raises:
        HTTPException: 404 if agent not found.
    """
    service = PromptTraceService(
        event_store,
        PromptParser(
            extra_tag_mappings=domain_plugin.get_tag_mappings() if domain_plugin else None,
            extra_provenance_patterns=(
                domain_plugin.get_provenance_patterns() if domain_plugin else None
            ),
        ),
    )
    agent_node = await service.trace_single_agent(agent_id)

    if not agent_node:
        raise HTTPException(
            status_code=404, detail=f"Agent {agent_id} not found"
        )

    return _agent_node_to_schema(agent_node)
