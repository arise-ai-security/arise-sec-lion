"""Prompt trace service for building hierarchy traces.

Orchestrates event querying and prompt parsing to build a complete
hierarchy trace. Uses ports for infrastructure abstraction.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.application.services.prompt.prompt_parser import PromptParser
from core.domain.events.events import (
    AgentCreated,
    ComplexityEvaluated,
    DomainEvent,
    PromptSent,
    TaskAssigned,
)
from core.domain.values.prompt_trace import (
    AgentNode,
    HierarchyTrace,
    ParsedPrompt,
)


if TYPE_CHECKING:
    from core.ports.event_store_port import EventStoreReadPort


class PromptTraceService:
    """Builds hierarchy traces from events.

    Orchestrates:
    1. Querying events via EventStoreReadPort
    2. Building the agent tree structure
    3. Parsing prompts via PromptParser

    No database or rendering logic - pure application orchestration.
    """

    def __init__(
        self,
        event_store: "EventStoreReadPort",
        parser: PromptParser | None = None,
    ) -> None:
        """Initialize the trace service.

        Args:
            event_store: Event store for reading events (via port).
            parser: Optional prompt parser (defaults to new instance).
        """
        self._store = event_store
        self._parser = parser or PromptParser()

    async def trace(self, root_id: UUID) -> HierarchyTrace:
        """Build complete hierarchy trace from root agent.

        Args:
            root_id: Root agent (BOSS) UUID to trace from.

        Returns:
            HierarchyTrace with full tree and statistics.
        """
        # Get all events for this hierarchy using optimized CTE query
        grouped_events = await self._store.get_hierarchy_events_grouped(root_id)

        if not grouped_events:
            return HierarchyTrace(
                root=AgentNode(
                    agent_id=root_id,
                    role="unknown",
                    depth=0,
                    task="No events found",
                    sibling_index=0,
                    prompts=(),
                    children=(),
                ),
                total_agents=0,
                max_depth=0,
            )

        agent_data = self._extract_agent_data(grouped_events)

        children_map = self._build_children_map(agent_data)

        root = self._build_tree(root_id, agent_data, children_map, depth=0)

        return HierarchyTrace(
            root=root,
            total_agents=self._count_agents(root),
            max_depth=self._max_depth(root),
        )

    def _extract_agent_data(
        self,
        grouped_events: dict[UUID, list[DomainEvent]],
    ) -> dict[UUID, dict]:
        """Extract agent data from events.

        Returns dict mapping agent_id to:
        - role: agent role
        - task: task description
        - parent_id: parent agent ID
        - sibling_index: position among siblings
        - prompts: list of ParsedPrompt objects
        """
        agent_data: dict[UUID, dict] = {}

        for agent_id, events in grouped_events.items():
            data: dict[str, Any] = {
                "role": "unknown",
                "task": "",
                "parent_id": None,
                "sibling_index": 0,
                "prompts": [],
            }

            for event in events:
                if isinstance(event, AgentCreated):
                    data["role"] = event.role
                    data["parent_id"] = event.parent_id
                    data["sibling_index"] = event.sibling_index
                elif isinstance(event, ComplexityEvaluated):
                    # Update role from complexity evaluation (PENDING → WORKER/MANAGER)
                    data["role"] = event.determined_role
                elif isinstance(event, TaskAssigned):
                    data["task"] = event.task_description
                elif isinstance(event, PromptSent):
                    # Parse prompt and create ParsedPrompt
                    sections = self._parser.parse(event.prompt)
                    parsed = ParsedPrompt(
                        raw=event.prompt,
                        occurred_at=event.occurred_at,
                        prompt_type=event.prompt_type,
                        target=event.target,
                        sections=sections,
                    )
                    data["prompts"].append(parsed)

            agent_data[agent_id] = data

        return agent_data

    def _build_children_map(
        self,
        agent_data: dict[UUID, dict],
    ) -> dict[UUID | None, list[UUID]]:
        children_map: dict[UUID | None, list[UUID]] = {}

        for agent_id, data in agent_data.items():
            parent_id = data["parent_id"]
            if parent_id not in children_map:
                children_map[parent_id] = []
            children_map[parent_id].append(agent_id)

        # Sort children by sibling_index for left-to-right ordering
        for children in children_map.values():
            children.sort(key=lambda aid: agent_data[aid]["sibling_index"])

        return children_map

    def _build_tree(
        self,
        agent_id: UUID,
        agent_data: dict[UUID, dict],
        children_map: dict[UUID | None, list[UUID]],
        depth: int,
    ) -> AgentNode:
        data = agent_data.get(agent_id, {})

        child_ids = children_map.get(agent_id, [])
        children = tuple(
            self._build_tree(child_id, agent_data, children_map, depth + 1)
            for child_id in child_ids
        )

        return AgentNode(
            agent_id=agent_id,
            role=data.get("role", "unknown"),
            depth=depth,
            task=data.get("task", ""),
            sibling_index=data.get("sibling_index", 0),
            prompts=tuple(data.get("prompts", [])),
            children=children,
        )

    def _count_agents(self, node: AgentNode) -> int:
        return 1 + sum(self._count_agents(child) for child in node.children)

    def _max_depth(self, node: AgentNode) -> int:
        if not node.children:
            return node.depth
        return max(self._max_depth(child) for child in node.children)

    async def trace_single_agent(self, agent_id: UUID) -> AgentNode | None:
        """Build trace for a single agent without fetching entire hierarchy.

        Optimized: Fetches only the target agent's events (1 query instead of
        fetching entire hierarchy and searching).

        Args:
            agent_id: UUID of the agent to trace.

        Returns:
            AgentNode for the single agent, or None if not found.
        """
        events = await self._store.get_events(agent_id)
        if not events:
            return None

        # Extract data from events
        role = "unknown"
        task = ""
        sibling_index = 0
        prompts: list[ParsedPrompt] = []

        for event in events:
            if isinstance(event, AgentCreated):
                role = event.role
                sibling_index = event.sibling_index
            elif isinstance(event, ComplexityEvaluated):
                # Update role from complexity evaluation (PENDING → WORKER/MANAGER)
                role = event.determined_role
            elif isinstance(event, TaskAssigned):
                task = event.task_description
            elif isinstance(event, PromptSent):
                sections = self._parser.parse(event.prompt)
                prompts.append(ParsedPrompt(
                    raw=event.prompt,
                    occurred_at=event.occurred_at,
                    prompt_type=event.prompt_type,
                    target=event.target,
                    sections=sections,
                ))

        return AgentNode(
            agent_id=agent_id,
            role=role,
            depth=0,  # Unknown without hierarchy context
            task=task,
            sibling_index=sibling_index,
            prompts=tuple(prompts),
            children=(),  # No children in single-agent view
        )
