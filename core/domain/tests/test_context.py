"""Test cases for context value objects (Briefing, Report, Ancestor).

Tests the serialization/deserialization and factory methods.
"""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.values.context import HierarchyLimits
from core.domain.values.node_message import (
    Ancestor,
    Briefing,
    Report,
    build_briefing,
)


class TestAncestor:
    """Tests for Ancestor value object."""

    def test_creation(self) -> None:
        """Test creating Ancestor directly."""
        info = Ancestor(
            agent_id="123",
            role="boss",
            task_summary="Build a web app",
        )
        assert info.agent_id == "123"
        assert info.role == "boss"
        assert info.task_summary == "Build a web app"

    def test_from_agent(self) -> None:
        """Test creating Ancestor from AgentSession."""
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

        info = Ancestor.from_agent(agent)

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
        long_task = "A" * 400  # 400 chars
        agent.assign_task(long_task)

        info = Ancestor.from_agent(agent)

        assert len(info.task_summary) == 300
        assert info.task_summary == "A" * 300

    def test_detailed_task_preserves_critical_details(self) -> None:
        """A detailed task must preserve key identifiers, file paths, and function names.

        The cap is 300 chars so child agents receive enough ancestry context
        for proper alignment.
        """
        agent_id = uuid4()
        config = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        agent = AgentSession.create(
            agent_id=agent_id,
            role=AgentRole.MANAGER,
            config=config,
        )
        # Realistic multi-part task (typical length ~200 chars)
        task = (
            "Analyze issue PROJ-4820: incorrect ordering in the "
            "checkout module CartReconciler at cart_service.py:388. "
            "Reproduce the failure, develop a targeted fix that "
            "restores correct ordering, and verify the fix eliminates "
            "the failure under test."
        )
        agent.assign_task(task)
        info = Ancestor.from_agent(agent)

        # All critical details must survive truncation
        assert "PROJ-4820" in info.task_summary
        assert "CartReconciler" in info.task_summary
        assert "cart_service.py:388" in info.task_summary
        assert "incorrect ordering" in info.task_summary

    def test_serialization_roundtrip(self) -> None:
        """Test model_dump and model_validate roundtrip."""
        info = Ancestor(
            agent_id="123",
            role="manager",
            task_summary="Some task",
        )
        data = info.model_dump()
        restored = Ancestor.model_validate(data)

        assert restored == info


class TestBriefing:
    """Tests for Briefing value object."""

    def test_creation(self) -> None:
        """Test creating Briefing directly."""
        briefing = Briefing(
            parent_task="Build web app",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id="1", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React",),
        )

        assert briefing.parent_task == "Build web app"
        assert briefing.parent_role == "boss"
        assert len(briefing.ancestry) == 1
        assert len(briefing.decisions) == 1

    def test_serialization_roundtrip(self) -> None:
        """Test model_dump and model_validate roundtrip."""
        briefing = Briefing(
            parent_task="Build web app",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id="1", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React", "Use PostgreSQL"),
            evidence_references=("artifact:analysis.txt",),
        )

        data = briefing.model_dump()
        restored = Briefing.model_validate(data)

        assert restored.parent_task == briefing.parent_task
        assert restored.parent_role == briefing.parent_role
        assert len(restored.ancestry) == len(briefing.ancestry)
        assert restored.decisions == briefing.decisions
        assert restored.evidence_references == briefing.evidence_references


class TestReport:
    """Tests for Report value object."""

    def test_creation(self) -> None:
        """Test creating Report directly."""
        report = Report(
            result="Task completed successfully",
            task="Build web app",
            artifacts=("output.json", "report.md"),
            decisions=("Used library X",),
            execution_summary={"cost_usd": 0.05, "duration_seconds": 120},
        )

        assert report.result == "Task completed successfully"
        assert report.task == "Build web app"
        assert len(report.artifacts) == 2
        assert len(report.decisions) == 1
        assert report.execution_summary["cost_usd"] == 0.05

    def test_simple_factory(self) -> None:
        """Test Report.simple() factory."""
        report = Report.simple("Just a result")

        assert report.result == "Just a result"
        assert report.artifacts == ()
        assert report.decisions == ()
        assert report.execution_summary == {}

    def test_serialization_roundtrip(self) -> None:
        """Test model_dump and model_validate roundtrip."""
        report = Report(
            result="Done",
            task="Some task",
            artifacts=("a.txt",),
            decisions=("chose X",),
            execution_summary={"cost_usd": 1.0},
        )

        data = report.model_dump()
        restored = Report.model_validate(data)

        assert restored.result == report.result
        assert restored.task == report.task
        assert restored.artifacts == report.artifacts
        assert restored.decisions == report.decisions
        assert restored.execution_summary == report.execution_summary


class TestBuildBriefing:
    """Tests for build_briefing helper function."""

    def test_build_for_root_agent(self) -> None:
        """Test building briefing from a root agent with no parent."""
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

        briefing = build_briefing(agent, parent_briefing=None)

        assert briefing.parent_task == "Root task"
        assert briefing.parent_role == "boss"
        assert len(briefing.ancestry) == 1
        assert briefing.ancestry[0].role == "boss"

    def test_build_with_existing_ancestry(self) -> None:
        """Test building briefing with existing parent briefing."""
        child_id = uuid4()
        config = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        # Simulate briefing from spawning parent
        parent_briefing = Briefing(
            parent_task="Root task",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id="root-id", role="boss", task_summary="Root task"),
            ),
            decisions=("Use React",),
        )
        # Pass briefing directly to create() (persisted in AgentCreated event)
        child = AgentSession.create(
            agent_id=child_id,
            role=AgentRole.MANAGER,
            config=config,
            briefing=parent_briefing.model_dump(),
        )
        child.assign_task("Child task")

        # Set hierarchy limits (incremented depth)
        limits = HierarchyLimits(
            current_depth=1,
            max_depth=5,
            max_children_per_node=10,
            max_retries=3,
            root_id=uuid4(),
        )
        child.set_hierarchy_limits(limits)

        briefing = build_briefing(child, parent_briefing=parent_briefing)

        assert briefing.parent_task == "Child task"
        assert briefing.parent_role == "manager"
        # Ancestry should include parent + this agent
        assert len(briefing.ancestry) == 2
        assert briefing.ancestry[0].role == "boss"
        assert briefing.ancestry[1].role == "manager"
