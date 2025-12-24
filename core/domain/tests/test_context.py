"""Test cases for context value objects (ParentContext, ChildResult, AncestorInfo).

Tests the serialization/deserialization and factory methods.
"""

from uuid import uuid4

from core.domain.context import (
    AncestorInfo,
    ChildResult,
    ParentContext,
    build_parent_context,
)
from core.domain.execution_context import ExecutionContext
from core.domain.model import AgentRole, AgentSession


class TestAncestorInfo:
    """Tests for AncestorInfo value object."""

    def test_creation(self) -> None:
        """Test creating AncestorInfo directly."""
        info = AncestorInfo(
            agent_id="123",
            role="boss",
            task_summary="Build a web app",
        )
        assert info.agent_id == "123"
        assert info.role == "boss"
        assert info.task_summary == "Build a web app"

    def test_from_agent(self) -> None:
        """Test creating AncestorInfo from AgentSession."""
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

        info = AncestorInfo.from_agent(agent)

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

        info = AncestorInfo.from_agent(agent)

        assert len(info.task_summary) == 100
        assert info.task_summary == "A" * 100

    def test_serialization_roundtrip(self) -> None:
        """Test to_dict and from_dict roundtrip."""
        info = AncestorInfo(
            agent_id="123",
            role="manager",
            task_summary="Some task",
        )
        data = info.to_dict()
        restored = AncestorInfo.from_dict(data)

        assert restored == info


class TestParentContext:
    """Tests for ParentContext value object."""

    def test_creation(self) -> None:
        """Test creating ParentContext directly."""
        context = ParentContext(
            parent_task="Build web app",
            parent_role="boss",
            depth=1,
            ancestry=(
                AncestorInfo(agent_id="1", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React",),
            constraints={"max_cost": 100},
            execution_limits={"depth_remaining": 3},
        )

        assert context.parent_task == "Build web app"
        assert context.parent_role == "boss"
        assert context.depth == 1
        assert len(context.ancestry) == 1
        assert len(context.decisions) == 1
        assert context.constraints == {"max_cost": 100}

    def test_serialization_roundtrip(self) -> None:
        """Test to_dict and from_dict roundtrip."""
        context = ParentContext(
            parent_task="Build web app",
            parent_role="boss",
            depth=1,
            ancestry=(
                AncestorInfo(agent_id="1", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React", "Use PostgreSQL"),
            constraints={"max_cost": 100},
            execution_limits={"depth_remaining": 3, "max_children_per_node": 5},
        )

        data = context.to_dict()
        restored = ParentContext.from_dict(data)

        assert restored.parent_task == context.parent_task
        assert restored.parent_role == context.parent_role
        assert restored.depth == context.depth
        assert len(restored.ancestry) == len(context.ancestry)
        assert restored.decisions == context.decisions
        assert restored.constraints == context.constraints
        assert restored.execution_limits == context.execution_limits


class TestChildResult:
    """Tests for ChildResult value object."""

    def test_creation(self) -> None:
        """Test creating ChildResult directly."""
        result = ChildResult(
            result_text="Task completed successfully",
            artifacts=("output.json", "report.md"),
            decisions=("Used library X",),
            context_updates={"config.debug": True},
            execution_summary={"cost_usd": 0.05, "duration_seconds": 120},
        )

        assert result.result_text == "Task completed successfully"
        assert len(result.artifacts) == 2
        assert len(result.decisions) == 1
        assert result.execution_summary["cost_usd"] == 0.05

    def test_simple_factory(self) -> None:
        """Test ChildResult.simple() factory."""
        result = ChildResult.simple("Just a result")

        assert result.result_text == "Just a result"
        assert result.artifacts == ()
        assert result.decisions == ()
        assert result.context_updates == {}
        assert result.execution_summary == {}

    def test_serialization_roundtrip(self) -> None:
        """Test to_dict and from_dict roundtrip."""
        result = ChildResult(
            result_text="Done",
            artifacts=("a.txt",),
            decisions=("chose X",),
            context_updates={"key": "value"},
            execution_summary={"cost_usd": 1.0},
        )

        data = result.to_dict()
        restored = ChildResult.from_dict(data)

        assert restored.result_text == result.result_text
        assert restored.artifacts == result.artifacts
        assert restored.decisions == result.decisions
        assert restored.context_updates == result.context_updates
        assert restored.execution_summary == result.execution_summary


class TestBuildParentContext:
    """Tests for build_parent_context helper function."""

    def test_build_for_root_agent(self) -> None:
        """Test building context from a root agent with no parent."""
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

        # Set execution context
        exec_ctx = ExecutionContext.create_root(
            root_id=root_id,
            max_depth=5,
            max_children_per_node=10,
            max_retries=3,
        )
        agent.set_execution_context(exec_ctx)

        context = build_parent_context(agent, parent_context=None)

        assert context.parent_task == "Root task"
        assert context.parent_role == "boss"
        assert context.depth == 0
        assert len(context.ancestry) == 1
        assert context.ancestry[0].role == "boss"
        assert context.execution_limits["depth_remaining"] == 5
        assert context.execution_limits["max_children_per_node"] == 10

    def test_build_with_existing_ancestry(self) -> None:
        """Test building context with existing parent context."""
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

        # Simulate parent context from spawning
        parent_ctx = ParentContext(
            parent_task="Root task",
            parent_role="boss",
            depth=0,
            ancestry=(
                AncestorInfo(agent_id="root-id", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React",),
            constraints={},
            execution_limits={"depth_remaining": 4},
        )
        child.set_parent_context(parent_ctx)

        # Set execution context (incremented depth)
        exec_ctx = ExecutionContext(
            current_depth=1,
            max_depth=5,
            max_children_per_node=10,
            max_retries=3,
            root_id=uuid4(),
        )
        child.set_execution_context(exec_ctx)

        context = build_parent_context(child, parent_context=parent_ctx)

        assert context.parent_task == "Child task"
        assert context.parent_role == "manager"
        assert context.depth == 1
        # Ancestry should include parent + this agent
        assert len(context.ancestry) == 2
        assert context.ancestry[0].role == "boss"
        assert context.ancestry[1].role == "manager"
