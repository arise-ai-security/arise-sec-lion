"""Test cases for context value objects (SpawnPayload, TaskOutcome, AncestorSummary).

Tests the serialization/deserialization and factory methods.
"""

from uuid import uuid4

from core.domain.values.context import (
    AncestorSummary,
    TaskOutcome,
    SpawnPayload,
    HierarchyLimits,
    build_spawn_payload,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession


class TestAncestorSummary:
    """Tests for AncestorSummary value object."""

    def test_creation(self) -> None:
        """Test creating AncestorSummary directly."""
        info = AncestorSummary(
            agent_id="123",
            role="boss",
            task_summary="Build a web app",
        )
        assert info.agent_id == "123"
        assert info.role == "boss"
        assert info.task_summary == "Build a web app"

    def test_from_agent(self) -> None:
        """Test creating AncestorSummary from AgentSession."""
        agent_id = uuid4()
        config = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        agent = AgentSession.create(
            agent_id=agent_id,
            role=AgentRole.BOSS,
            config=config,
        )
        agent.assign_task("Build a comprehensive web application")

        info = AncestorSummary.from_agent(agent)

        assert info.agent_id == str(agent_id)
        assert info.role == "boss"
        assert info.task_summary == "Build a comprehensive web application"

    def test_task_summary_truncation(self) -> None:
        """Test that task summary is truncated to 100 chars."""
        agent_id = uuid4()
        config = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        agent = AgentSession.create(
            agent_id=agent_id,
            role=AgentRole.BOSS,
            config=config,
        )
        long_task = "A" * 200  # 200 chars
        agent.assign_task(long_task)

        info = AncestorSummary.from_agent(agent)

        assert len(info.task_summary) == 100
        assert info.task_summary == "A" * 100

    def test_serialization_roundtrip(self) -> None:
        """Test model_dump and model_validate roundtrip."""
        info = AncestorSummary(
            agent_id="123",
            role="manager",
            task_summary="Some task",
        )
        data = info.model_dump()
        restored = AncestorSummary.model_validate(data)

        assert restored == info


class TestSpawnPayload:
    """Tests for SpawnPayload value object."""

    def test_creation(self) -> None:
        """Test creating SpawnPayload directly."""
        payload = SpawnPayload(
            parent_task="Build web app",
            parent_role="boss",
            depth=1,
            ancestry=(
                AncestorSummary(agent_id="1", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React",),
            constraints={"max_cost": 100},
            execution_limits={"depth_remaining": 3},
        )

        assert payload.parent_task == "Build web app"
        assert payload.parent_role == "boss"
        assert payload.depth == 1
        assert len(payload.ancestry) == 1
        assert len(payload.decisions) == 1
        assert payload.constraints == {"max_cost": 100}

    def test_serialization_roundtrip(self) -> None:
        """Test model_dump and model_validate roundtrip."""
        payload = SpawnPayload(
            parent_task="Build web app",
            parent_role="boss",
            depth=1,
            ancestry=(
                AncestorSummary(agent_id="1", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React", "Use PostgreSQL"),
            constraints={"max_cost": 100},
            execution_limits={"depth_remaining": 3, "max_children_per_node": 5},
        )

        data = payload.model_dump()
        restored = SpawnPayload.model_validate(data)

        assert restored.parent_task == payload.parent_task
        assert restored.parent_role == payload.parent_role
        assert restored.depth == payload.depth
        assert len(restored.ancestry) == len(payload.ancestry)
        assert restored.decisions == payload.decisions
        assert restored.constraints == payload.constraints
        assert restored.execution_limits == payload.execution_limits


class TestTaskOutcome:
    """Tests for TaskOutcome value object."""

    def test_creation(self) -> None:
        """Test creating TaskOutcome directly."""
        outcome = TaskOutcome(
            result_text="Task completed successfully",
            artifacts=("output.json", "report.md"),
            decisions=("Used library X",),
            context_updates={"config.debug": True},
            execution_summary={"cost_usd": 0.05, "duration_seconds": 120},
        )

        assert outcome.result_text == "Task completed successfully"
        assert len(outcome.artifacts) == 2
        assert len(outcome.decisions) == 1
        assert outcome.execution_summary["cost_usd"] == 0.05

    def test_simple_factory(self) -> None:
        """Test TaskOutcome.simple() factory."""
        outcome = TaskOutcome.simple("Just a result")

        assert outcome.result_text == "Just a result"
        assert outcome.artifacts == ()
        assert outcome.decisions == ()
        assert outcome.context_updates == {}
        assert outcome.execution_summary == {}

    def test_serialization_roundtrip(self) -> None:
        """Test model_dump and model_validate roundtrip."""
        outcome = TaskOutcome(
            result_text="Done",
            artifacts=("a.txt",),
            decisions=("chose X",),
            context_updates={"key": "value"},
            execution_summary={"cost_usd": 1.0},
        )

        data = outcome.model_dump()
        restored = TaskOutcome.model_validate(data)

        assert restored.result_text == outcome.result_text
        assert restored.artifacts == outcome.artifacts
        assert restored.decisions == outcome.decisions
        assert restored.context_updates == outcome.context_updates
        assert restored.execution_summary == outcome.execution_summary


class TestBuildSpawnPayload:
    """Tests for build_spawn_payload helper function."""

    def test_build_for_root_agent(self) -> None:
        """Test building payload from a root agent with no parent."""
        root_id = uuid4()
        config = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        agent = AgentSession.create(
            agent_id=root_id,
            role=AgentRole.BOSS,
            config=config,
        )
        agent.assign_task("Root task")

        # Set hierarchy limits
        limits = HierarchyLimits.create_root(
            root_id=root_id,
            max_depth=5,
            max_children_per_node=10,
            max_retries=3,
        )
        agent.set_hierarchy_limits(limits)

        payload = build_spawn_payload(agent, parent_payload=None)

        assert payload.parent_task == "Root task"
        assert payload.parent_role == "boss"
        assert payload.depth == 0
        assert len(payload.ancestry) == 1
        assert payload.ancestry[0].role == "boss"
        assert payload.execution_limits["depth_remaining"] == 5
        assert payload.execution_limits["max_children_per_node"] == 10

    def test_build_with_existing_ancestry(self) -> None:
        """Test building payload with existing spawn payload."""
        child_id = uuid4()
        config = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        child = AgentSession.create(
            agent_id=child_id,
            role=AgentRole.MANAGER,
            config=config,
        )
        child.assign_task("Child task")

        # Simulate spawn payload from spawning
        parent_payload = SpawnPayload(
            parent_task="Root task",
            parent_role="boss",
            depth=0,
            ancestry=(
                AncestorSummary(agent_id="root-id", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React",),
            constraints={},
            execution_limits={"depth_remaining": 4},
        )
        child.set_spawn_payload(parent_payload)

        # Set hierarchy limits (incremented depth)
        limits = HierarchyLimits(
            current_depth=1,
            max_depth=5,
            max_children_per_node=10,
            max_retries=3,
            root_id=uuid4(),
        )
        child.set_hierarchy_limits(limits)

        payload = build_spawn_payload(child, parent_payload=parent_payload)

        assert payload.parent_task == "Child task"
        assert payload.parent_role == "manager"
        assert payload.depth == 1
        # Ancestry should include parent + this agent
        assert len(payload.ancestry) == 2
        assert payload.ancestry[0].role == "boss"
        assert payload.ancestry[1].role == "manager"
