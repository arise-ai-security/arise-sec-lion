"""Test cases for sibling context value objects."""

import pytest

from core.domain.values.sibling_context import (
    DecisionInfo,
    SiblingTaskInfo,
    WorkerSiblingContext,
)


class TestSiblingTaskInfo:
    """Test cases for SiblingTaskInfo value object."""

    def test_create_sibling_task_info(self) -> None:
        """Test creating SiblingTaskInfo instance."""
        info = SiblingTaskInfo(
            agent_id="agent-123",
            sibling_index=0,
            status="completed",
            task_summary="Fix authentication bug",
            result_summary="Fixed the bug in auth.py",
        )

        assert info.agent_id == "agent-123"
        assert info.sibling_index == 0
        assert info.status == "completed"
        assert info.task_summary == "Fix authentication bug"
        assert info.result_summary == "Fixed the bug in auth.py"

    def test_to_dict_serialization(self) -> None:
        """Test to_dict() serialization."""
        info = SiblingTaskInfo(
            agent_id="agent-123",
            sibling_index=1,
            status="in_progress",
            task_summary="Write tests",
            result_summary=None,
        )

        data = info.to_dict()

        assert data["agent_id"] == "agent-123"
        assert data["sibling_index"] == 1
        assert data["status"] == "in_progress"
        assert data["task_summary"] == "Write tests"
        assert data["result_summary"] is None

    def test_from_dict_deserialization(self) -> None:
        """Test from_dict() deserialization."""
        data = {
            "agent_id": "agent-456",
            "sibling_index": 2,
            "status": "pending",
            "task_summary": "Deploy application",
            "result_summary": None,
        }

        info = SiblingTaskInfo.from_dict(data)

        assert info.agent_id == "agent-456"
        assert info.sibling_index == 2
        assert info.status == "pending"
        assert info.task_summary == "Deploy application"
        assert info.result_summary is None

    def test_immutability(self) -> None:
        """Test that SiblingTaskInfo is immutable (frozen dataclass)."""
        info = SiblingTaskInfo(
            agent_id="agent-123",
            sibling_index=0,
            status="pending",
            task_summary="Test task",
            result_summary=None,
        )

        with pytest.raises(AttributeError):
            info.status = "completed"  # type: ignore[misc]


class TestDecisionInfo:
    """Test cases for DecisionInfo value object."""

    def test_create_decision_info(self) -> None:
        """Test creating DecisionInfo instance."""
        info = DecisionInfo(
            key="api_framework",
            value="FastAPI",
            rationale="Better async support",
            decided_by="agent-123",
        )

        assert info.key == "api_framework"
        assert info.value == "FastAPI"
        assert info.rationale == "Better async support"
        assert info.decided_by == "agent-123"

    def test_to_dict_serialization(self) -> None:
        """Test to_dict() serialization."""
        info = DecisionInfo(
            key="orm_framework",
            value="SQLAlchemy",
            rationale="Type hints support",
            decided_by="agent-456",
        )

        data = info.to_dict()

        assert data["key"] == "orm_framework"
        assert data["value"] == "SQLAlchemy"
        assert data["rationale"] == "Type hints support"
        assert data["decided_by"] == "agent-456"

    def test_from_dict_deserialization(self) -> None:
        """Test from_dict() deserialization."""
        data = {
            "key": "testing_framework",
            "value": "pytest",
            "rationale": "Standard in Python",
            "decided_by": "agent-789",
        }

        info = DecisionInfo.from_dict(data)

        assert info.key == "testing_framework"
        assert info.value == "pytest"
        assert info.rationale == "Standard in Python"
        assert info.decided_by == "agent-789"


class TestWorkerSiblingContext:
    """Test cases for WorkerSiblingContext value object."""

    def test_create_empty_context(self) -> None:
        """Test creating context with no siblings or decisions."""
        context = WorkerSiblingContext(
            current_agent_id="agent-123",
            parent_task=None,
            sibling_tasks=(),
            shared_decisions=(),
        )

        assert context.current_agent_id == "agent-123"
        assert context.parent_task is None
        assert context.sibling_tasks == ()
        assert context.shared_decisions == ()
        assert context.total_siblings == 0
        assert context.completed_count == 0
        assert context.in_progress_count == 0

    def test_create_context_with_siblings(self) -> None:
        """Test creating context with sibling tasks."""
        sibling1 = SiblingTaskInfo(
            agent_id="sibling-1",
            sibling_index=0,
            status="completed",
            task_summary="First task",
            result_summary="Done",
        )
        sibling2 = SiblingTaskInfo(
            agent_id="sibling-2",
            sibling_index=1,
            status="in_progress",
            task_summary="Second task",
            result_summary=None,
        )
        sibling3 = SiblingTaskInfo(
            agent_id="sibling-3",
            sibling_index=3,
            status="pending",
            task_summary="Third task",
            result_summary=None,
        )

        context = WorkerSiblingContext(
            current_agent_id="agent-current",
            parent_task="Build REST API",
            sibling_tasks=(sibling1, sibling2, sibling3),
            shared_decisions=(),
        )

        assert context.total_siblings == 3
        assert context.completed_count == 1
        assert context.in_progress_count == 1

    def test_in_progress_includes_analyzing(self) -> None:
        """Test that in_progress_count includes 'analyzing' status."""
        sibling1 = SiblingTaskInfo(
            agent_id="sibling-1",
            sibling_index=0,
            status="analyzing",
            task_summary="Task 1",
            result_summary=None,
        )
        sibling2 = SiblingTaskInfo(
            agent_id="sibling-2",
            sibling_index=1,
            status="in_progress",
            task_summary="Task 2",
            result_summary=None,
        )

        context = WorkerSiblingContext(
            current_agent_id="agent-current",
            parent_task=None,
            sibling_tasks=(sibling1, sibling2),
            shared_decisions=(),
        )

        assert context.in_progress_count == 2

    def test_to_template_dict(self) -> None:
        """Test to_template_dict() for Jinja2 rendering."""
        sibling = SiblingTaskInfo(
            agent_id="sibling-1",
            sibling_index=0,
            status="completed",
            task_summary="First task",
            result_summary="Done",
        )
        decision = DecisionInfo(
            key="api_framework",
            value="FastAPI",
            rationale="Better async",
            decided_by="agent-123",
        )

        context = WorkerSiblingContext(
            current_agent_id="agent-current",
            parent_task="Build REST API",
            sibling_tasks=(sibling,),
            shared_decisions=(decision,),
        )

        template_dict = context.to_template_dict()

        assert template_dict["current_agent_id"] == "agent-current"
        assert template_dict["parent_task"] == "Build REST API"
        assert len(template_dict["sibling_tasks"]) == 1
        assert len(template_dict["shared_decisions"]) == 1
        assert template_dict["total_siblings"] == 1
        assert template_dict["completed_count"] == 1
        assert template_dict["in_progress_count"] == 0

    def test_roundtrip_serialization(self) -> None:
        """Test to_dict() -> from_dict() roundtrip."""
        sibling = SiblingTaskInfo(
            agent_id="sibling-1",
            sibling_index=0,
            status="completed",
            task_summary="First task",
            result_summary="Done",
        )
        decision = DecisionInfo(
            key="api_framework",
            value="FastAPI",
            rationale="Better async",
            decided_by="agent-123",
        )

        original = WorkerSiblingContext(
            current_agent_id="agent-current",
            parent_task="Build REST API",
            sibling_tasks=(sibling,),
            shared_decisions=(decision,),
        )

        data = original.to_dict()
        restored = WorkerSiblingContext.from_dict(data)

        assert restored.current_agent_id == original.current_agent_id
        assert restored.parent_task == original.parent_task
        assert len(restored.sibling_tasks) == len(original.sibling_tasks)
        assert len(restored.shared_decisions) == len(original.shared_decisions)
        assert restored.sibling_tasks[0].agent_id == original.sibling_tasks[0].agent_id
        assert restored.shared_decisions[0].key == original.shared_decisions[0].key
