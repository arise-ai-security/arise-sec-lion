"""Unit tests for PromptTraceService.

Tests hierarchy building, event processing, and tree construction.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from core.application.services.prompt_parser import PromptParser
from core.application.services.prompt_trace_service import PromptTraceService
from core.domain.events.events import (
    AgentCreated,
    PromptSent,
    TaskAssigned,
)
from core.domain.values.prompt_trace import SectionProvenance


def create_agent_created(
    agent_id: UUID,
    role: str,
    parent_id: UUID | None = None,
    sibling_index: int = 0,
) -> AgentCreated:
    """Helper to create AgentCreated events."""
    return AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=role,
        parent_id=parent_id,
        sibling_index=sibling_index,
        config={},
    )


def create_task_assigned(agent_id: UUID, task: str) -> TaskAssigned:
    """Helper to create TaskAssigned events."""
    return TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description=task,
    )


def create_prompt_sent(
    agent_id: UUID,
    prompt: str,
    prompt_type: str = "task_decomposition",
    target: str = "llm",
) -> PromptSent:
    """Helper to create PromptSent events."""
    return PromptSent(
        aggregate_id=agent_id,
        sequence_number=3,
        prompt=prompt,
        prompt_type=prompt_type,
        target=target,
    )


class TestPromptTraceServiceBasic:
    """Basic tests for PromptTraceService."""

    @pytest.fixture
    def mock_event_store(self):
        """Create a mock event store."""
        store = MagicMock()
        store.get_hierarchy_events_grouped = AsyncMock()
        return store

    @pytest.fixture
    def service(self, mock_event_store) -> PromptTraceService:
        """Create a PromptTraceService with mock dependencies."""
        parser = PromptParser()
        return PromptTraceService(mock_event_store, parser)

    @pytest.mark.asyncio
    async def test_trace_empty_hierarchy(self, service, mock_event_store) -> None:
        """Test tracing when no events are found."""
        root_id = uuid4()
        mock_event_store.get_hierarchy_events_grouped.return_value = {}

        trace = await service.trace(root_id)

        assert trace.total_agents == 0
        assert trace.max_depth == 0
        assert trace.root.agent_id == root_id
        assert trace.root.role == "unknown"

    @pytest.mark.asyncio
    async def test_trace_single_agent(self, service, mock_event_store) -> None:
        """Test tracing a single agent (boss only)."""
        root_id = uuid4()
        prompt = "<ROLE>Boss agent</ROLE><TASK>Fix CVE</TASK>"

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            root_id: [
                create_agent_created(root_id, "boss"),
                create_task_assigned(root_id, "Fix the CVE"),
                create_prompt_sent(root_id, prompt),
            ]
        }

        trace = await service.trace(root_id)

        assert trace.total_agents == 1
        assert trace.max_depth == 0
        assert trace.root.agent_id == root_id
        assert trace.root.role == "boss"
        assert trace.root.task == "Fix the CVE"
        assert len(trace.root.prompts) == 1
        assert len(trace.root.children) == 0

    @pytest.mark.asyncio
    async def test_trace_with_children(self, service, mock_event_store) -> None:
        """Test tracing a hierarchy with children."""
        root_id = uuid4()
        child1_id = uuid4()
        child2_id = uuid4()

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            root_id: [
                create_agent_created(root_id, "boss"),
                create_task_assigned(root_id, "Root task"),
            ],
            child1_id: [
                create_agent_created(child1_id, "worker", root_id, sibling_index=0),
                create_task_assigned(child1_id, "Child 1 task"),
            ],
            child2_id: [
                create_agent_created(child2_id, "worker", root_id, sibling_index=1),
                create_task_assigned(child2_id, "Child 2 task"),
            ],
        }

        trace = await service.trace(root_id)

        assert trace.total_agents == 3
        assert trace.max_depth == 1
        assert len(trace.root.children) == 2
        assert trace.root.children[0].sibling_index == 0
        assert trace.root.children[1].sibling_index == 1

    @pytest.mark.asyncio
    async def test_children_sorted_by_sibling_index(self, service, mock_event_store) -> None:
        """Test that children are sorted by sibling_index."""
        root_id = uuid4()
        child1_id = uuid4()  # sibling_index=1 (second)
        child2_id = uuid4()  # sibling_index=0 (first)

        # Note: Returning in wrong order to test sorting
        mock_event_store.get_hierarchy_events_grouped.return_value = {
            root_id: [create_agent_created(root_id, "boss")],
            child1_id: [create_agent_created(child1_id, "worker", root_id, sibling_index=1)],
            child2_id: [create_agent_created(child2_id, "worker", root_id, sibling_index=0)],
        }

        trace = await service.trace(root_id)

        # Children should be sorted: sibling_index=0 first, then sibling_index=1
        assert trace.root.children[0].agent_id == child2_id
        assert trace.root.children[1].agent_id == child1_id


class TestPromptTraceServicePromptParsing:
    """Tests for prompt parsing within the service."""

    @pytest.fixture
    def mock_event_store(self):
        store = MagicMock()
        store.get_hierarchy_events_grouped = AsyncMock()
        return store

    @pytest.fixture
    def service(self, mock_event_store) -> PromptTraceService:
        return PromptTraceService(mock_event_store, PromptParser())

    @pytest.mark.asyncio
    async def test_prompts_are_parsed(self, service, mock_event_store) -> None:
        """Test that prompts are parsed into sections."""
        root_id = uuid4()
        prompt = """
<ROLE>You are a BOSS agent.</ROLE>
<TASK>Fix the CVE</TASK>
<cve_instance>CVE-2023-1234</cve_instance>
"""
        mock_event_store.get_hierarchy_events_grouped.return_value = {
            root_id: [
                create_agent_created(root_id, "boss"),
                create_prompt_sent(root_id, prompt),
            ]
        }

        trace = await service.trace(root_id)

        assert len(trace.root.prompts) == 1
        parsed = trace.root.prompts[0]
        assert len(parsed.sections) == 3

        tags = {s.tag for s in parsed.sections}
        assert "ROLE" in tags
        assert "TASK" in tags
        assert "cve_instance" in tags

    @pytest.mark.asyncio
    async def test_multiple_prompts_per_agent(self, service, mock_event_store) -> None:
        """Test agent with multiple prompts (complexity + execution)."""
        root_id = uuid4()
        prompt1 = "<ROLE>Pending agent</ROLE>"
        prompt2 = "<ROLE>Worker agent</ROLE><TASK>Execute</TASK>"

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            root_id: [
                create_agent_created(root_id, "pending"),
                create_prompt_sent(root_id, prompt1, "complexity_evaluation"),
                create_prompt_sent(root_id, prompt2, "worker_execution"),
            ]
        }

        trace = await service.trace(root_id)

        assert len(trace.root.prompts) == 2
        assert trace.root.prompts[0].prompt_type == "complexity_evaluation"
        assert trace.root.prompts[1].prompt_type == "worker_execution"

    @pytest.mark.asyncio
    async def test_prompt_metadata_preserved(self, service, mock_event_store) -> None:
        """Test that prompt metadata (type, target) is preserved."""
        root_id = uuid4()

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            root_id: [
                create_agent_created(root_id, "worker"),
                create_prompt_sent(
                    root_id,
                    "<ROLE>Worker</ROLE>",
                    prompt_type="worker_execution",
                    target="claude_code",
                ),
            ]
        }

        trace = await service.trace(root_id)

        prompt = trace.root.prompts[0]
        assert prompt.prompt_type == "worker_execution"
        assert prompt.target == "claude_code"


class TestPromptTraceServiceDeepHierarchy:
    """Tests for deep hierarchy structures."""

    @pytest.fixture
    def mock_event_store(self):
        store = MagicMock()
        store.get_hierarchy_events_grouped = AsyncMock()
        return store

    @pytest.fixture
    def service(self, mock_event_store) -> PromptTraceService:
        return PromptTraceService(mock_event_store, PromptParser())

    @pytest.mark.asyncio
    async def test_three_level_hierarchy(self, service, mock_event_store) -> None:
        """Test tracing a 3-level hierarchy (boss -> manager -> worker)."""
        boss_id = uuid4()
        manager_id = uuid4()
        worker_id = uuid4()

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            boss_id: [
                create_agent_created(boss_id, "boss"),
                create_task_assigned(boss_id, "Root task"),
            ],
            manager_id: [
                create_agent_created(manager_id, "manager", boss_id, sibling_index=0),
                create_task_assigned(manager_id, "Manager task"),
            ],
            worker_id: [
                create_agent_created(worker_id, "worker", manager_id, sibling_index=0),
                create_task_assigned(worker_id, "Worker task"),
            ],
        }

        trace = await service.trace(boss_id)

        assert trace.total_agents == 3
        assert trace.max_depth == 2
        assert trace.root.role == "boss"
        assert trace.root.depth == 0
        assert trace.root.children[0].role == "manager"
        assert trace.root.children[0].depth == 1
        assert trace.root.children[0].children[0].role == "worker"
        assert trace.root.children[0].children[0].depth == 2

    @pytest.mark.asyncio
    async def test_wide_hierarchy(self, service, mock_event_store) -> None:
        """Test tracing a wide hierarchy (boss with many children)."""
        boss_id = uuid4()
        worker_ids = [uuid4() for _ in range(5)]

        events = {
            boss_id: [
                create_agent_created(boss_id, "boss"),
                create_task_assigned(boss_id, "Root task"),
            ]
        }
        for i, worker_id in enumerate(worker_ids):
            events[worker_id] = [
                create_agent_created(worker_id, "worker", boss_id, sibling_index=i),
                create_task_assigned(worker_id, f"Worker {i} task"),
            ]

        mock_event_store.get_hierarchy_events_grouped.return_value = events

        trace = await service.trace(boss_id)

        assert trace.total_agents == 6
        assert trace.max_depth == 1
        assert len(trace.root.children) == 5

        # Verify sibling order
        for i, child in enumerate(trace.root.children):
            assert child.sibling_index == i


class TestPromptTraceServiceCounters:
    """Tests for total_agents and max_depth calculations."""

    @pytest.fixture
    def mock_event_store(self):
        store = MagicMock()
        store.get_hierarchy_events_grouped = AsyncMock()
        return store

    @pytest.fixture
    def service(self, mock_event_store) -> PromptTraceService:
        return PromptTraceService(mock_event_store, PromptParser())

    @pytest.mark.asyncio
    async def test_total_agents_count(self, service, mock_event_store) -> None:
        """Test total_agents counts all agents in hierarchy."""
        # Create a tree: boss -> 2 managers -> 3 workers each = 1 + 2 + 6 = 9
        boss_id = uuid4()
        m1_id, m2_id = uuid4(), uuid4()
        w1_ids = [uuid4() for _ in range(3)]
        w2_ids = [uuid4() for _ in range(3)]

        events = {boss_id: [create_agent_created(boss_id, "boss")]}
        events[m1_id] = [create_agent_created(m1_id, "manager", boss_id, 0)]
        events[m2_id] = [create_agent_created(m2_id, "manager", boss_id, 1)]

        for i, wid in enumerate(w1_ids):
            events[wid] = [create_agent_created(wid, "worker", m1_id, i)]
        for i, wid in enumerate(w2_ids):
            events[wid] = [create_agent_created(wid, "worker", m2_id, i)]

        mock_event_store.get_hierarchy_events_grouped.return_value = events

        trace = await service.trace(boss_id)

        assert trace.total_agents == 9

    @pytest.mark.asyncio
    async def test_max_depth_calculation(self, service, mock_event_store) -> None:
        """Test max_depth finds the deepest node."""
        # Create unbalanced tree: boss -> manager -> worker -> worker (depth=3)
        #                              -> worker (depth=1)
        boss_id = uuid4()
        m_id = uuid4()
        w1_id = uuid4()
        w2_id = uuid4()
        w3_id = uuid4()  # direct child of boss

        events = {
            boss_id: [create_agent_created(boss_id, "boss")],
            m_id: [create_agent_created(m_id, "manager", boss_id, 0)],
            w1_id: [create_agent_created(w1_id, "manager", m_id, 0)],  # depth 2
            w2_id: [create_agent_created(w2_id, "worker", w1_id, 0)],  # depth 3
            w3_id: [create_agent_created(w3_id, "worker", boss_id, 1)],  # depth 1
        }

        mock_event_store.get_hierarchy_events_grouped.return_value = events

        trace = await service.trace(boss_id)

        assert trace.max_depth == 3
