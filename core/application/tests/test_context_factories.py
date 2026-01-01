"""Test cases for context factory functions.

Tests the factory functions that create ContextData from domain objects.
"""

from uuid import uuid4

from core.application.services.context_factories import (
    ancestor_data_from_agent,
    ancestry_chain_from_agents,
    parent_summary_from_agent,
    sibling_entry_from_agent,
    sibling_results_from_agents,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus


def _create_agent(
    role: AgentRole = AgentRole.MANAGER,
    task: str = "Test task",
    result: str | None = None,
    sibling_index: int = 0,
) -> AgentSession:
    """Helper to create an agent for testing."""
    agent_id = uuid4()
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }
    agent = AgentSession.create(
        agent_id=agent_id,
        role=role,
        config=config,
        sibling_index=sibling_index,
    )
    agent.assign_task(task)

    if result is not None:
        # Simulate completion by setting result directly (for testing only)
        agent.result = result
        agent.status = AgentStatus.COMPLETED

    return agent


class TestParentSummaryFromAgent:
    """Tests for parent_summary_from_agent factory."""

    def test_basic_creation(self) -> None:
        """Test creating ParentSummary from agent."""
        agent = _create_agent(task="Analyze code")
        summary = parent_summary_from_agent(agent)

        assert summary.task == "Analyze code"
        assert summary.role == "manager"
        assert summary.result is None
        assert summary.decisions == ()

    def test_with_result(self) -> None:
        """Test with completed agent that has result."""
        agent = _create_agent(task="Fix bug", result="Bug fixed successfully")
        summary = parent_summary_from_agent(agent)

        assert summary.task == "Fix bug"
        assert summary.result == "Bug fixed successfully"

    def test_result_truncation(self) -> None:
        """Test that long results are truncated."""
        long_result = "A" * 1000
        agent = _create_agent(result=long_result)
        summary = parent_summary_from_agent(agent, result_limit=100)

        assert len(summary.result) == 100

    def test_with_local_decisions(self) -> None:
        """Test with agent that has local decisions."""
        agent = _create_agent()
        agent.record_decision("Use library X")
        agent.record_decision("Prefer async")

        summary = parent_summary_from_agent(agent)

        assert len(summary.decisions) == 2
        assert "Use library X" in summary.decisions
        assert "Prefer async" in summary.decisions


class TestAncestorDataFromAgent:
    """Tests for ancestor_data_from_agent factory."""

    def test_basic_creation_with_label(self) -> None:
        """Test creating AncestorData with custom label."""
        agent = _create_agent(role=AgentRole.BOSS, task="Main goal")
        ancestor = ancestor_data_from_agent(agent, label="boss_goal", depth=0)

        assert ancestor.label == "boss_goal"
        assert ancestor.role == "boss"
        assert ancestor.task == "Main goal"
        assert ancestor.depth == 0
        assert ancestor.template_key == "ancestor_boss_goal"

    def test_with_result_and_decisions(self) -> None:
        """Test with result and decisions."""
        agent = _create_agent(result="Completed analysis")
        agent.record_decision("Key decision")

        ancestor = ancestor_data_from_agent(agent, label="analyzer", depth=2)

        assert ancestor.result == "Completed analysis"
        assert len(ancestor.decisions) == 1


class TestAncestryChainFromAgents:
    """Tests for ancestry_chain_from_agents factory."""

    def test_empty_chain(self) -> None:
        """Test with empty list."""
        chain = ancestry_chain_from_agents([])
        assert len(chain.ancestors) == 0

    def test_single_ancestor(self) -> None:
        """Test with single ancestor."""
        boss = _create_agent(role=AgentRole.BOSS, task="Root task")
        chain = ancestry_chain_from_agents([boss])

        assert len(chain.ancestors) == 1
        assert chain.ancestors[0].role == "boss"
        assert chain.ancestors[0].depth == 0

    def test_multiple_ancestors(self) -> None:
        """Test with multiple ancestors."""
        boss = _create_agent(role=AgentRole.BOSS, task="Root")
        manager1 = _create_agent(role=AgentRole.MANAGER, task="Sub1")
        manager2 = _create_agent(role=AgentRole.MANAGER, task="Sub2")

        chain = ancestry_chain_from_agents([boss, manager1, manager2])

        assert len(chain.ancestors) == 3
        assert chain.ancestors[0].depth == 0
        assert chain.ancestors[1].depth == 1
        assert chain.ancestors[2].depth == 2

    def test_task_truncation(self) -> None:
        """Test that long tasks are truncated."""
        agent = _create_agent(task="A" * 200)
        chain = ancestry_chain_from_agents([agent], task_limit=50)

        assert len(chain.ancestors[0].task_summary) == 50


class TestSiblingResultsFromAgents:
    """Tests for sibling_results_from_agents factory."""

    def test_empty_list(self) -> None:
        """Test with empty sibling list."""
        results = sibling_results_from_agents([])
        assert len(results.siblings) == 0

    def test_excludes_self(self) -> None:
        """Test that exclude_id is respected."""
        agent1 = _create_agent(sibling_index=0)
        agent2 = _create_agent(sibling_index=1)
        agent3 = _create_agent(sibling_index=2)

        results = sibling_results_from_agents(
            [agent1, agent2, agent3],
            exclude_id=agent2.agent_id,
        )

        assert len(results.siblings) == 2
        # agent2 should be excluded
        agent_ids = [s.agent_id for s in results.siblings]
        assert str(agent2.agent_id) not in agent_ids

    def test_sorted_by_sibling_index(self) -> None:
        """Test that results are sorted by sibling index."""
        agent0 = _create_agent(sibling_index=0)
        agent1 = _create_agent(sibling_index=1)
        agent2 = _create_agent(sibling_index=2)

        # Pass in wrong order
        results = sibling_results_from_agents([agent2, agent0, agent1])

        assert results.siblings[0].index == 0
        assert results.siblings[1].index == 1
        assert results.siblings[2].index == 2

    def test_includes_status(self) -> None:
        """Test that status is included."""
        agent = _create_agent(result="Done")  # This sets status to COMPLETED

        results = sibling_results_from_agents([agent])

        assert results.siblings[0].status == "completed"

    def test_includes_result_summary(self) -> None:
        """Test that completed siblings have result summary."""
        agent = _create_agent(result="Task completed with output X")

        results = sibling_results_from_agents([agent])

        assert results.siblings[0].result_summary == "Task completed with output X"


class TestSiblingEntryFromAgent:
    """Tests for sibling_entry_from_agent factory."""

    def test_basic_creation(self) -> None:
        """Test creating single sibling entry."""
        agent = _create_agent(task="Worker task", sibling_index=3)
        entry = sibling_entry_from_agent(agent)

        assert entry.agent_id == str(agent.agent_id)
        assert entry.index == 3
        assert entry.task_summary == "Worker task"

    def test_with_result(self) -> None:
        """Test with completed agent."""
        agent = _create_agent(result="Finished work")
        entry = sibling_entry_from_agent(agent)

        assert entry.status == "completed"
        assert entry.result_summary == "Finished work"
