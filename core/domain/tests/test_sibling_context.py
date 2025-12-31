"""Test cases for sibling context value objects."""

import pytest

from core.domain.values.context import (
    SharedDecision,
    SiblingStatus,
    SiblingView,
)


class TestSiblingStatus:
    """Test cases for SiblingStatus value object."""

    def test_create_sibling_status(self) -> None:
        """Test creating SiblingStatus instance."""
        status = SiblingStatus(
            agent_id="agent-123",
            sibling_index=0,
            status="completed",
            task_summary="Fix authentication bug",
            result_summary="Fixed the bug in auth.py",
        )

        assert status.agent_id == "agent-123"
        assert status.sibling_index == 0
        assert status.status == "completed"
        assert status.task_summary == "Fix authentication bug"
        assert status.result_summary == "Fixed the bug in auth.py"

    def test_model_dump_serialization(self) -> None:
        """Test model_dump() serialization."""
        status = SiblingStatus(
            agent_id="agent-123",
            sibling_index=1,
            status="in_progress",
            task_summary="Write tests",
            result_summary=None,
        )

        data = status.model_dump()

        assert data["agent_id"] == "agent-123"
        assert data["sibling_index"] == 1
        assert data["status"] == "in_progress"
        assert data["task_summary"] == "Write tests"
        assert data["result_summary"] is None

    def test_model_validate_deserialization(self) -> None:
        """Test model_validate() deserialization."""
        data = {
            "agent_id": "agent-456",
            "sibling_index": 2,
            "status": "pending",
            "task_summary": "Deploy application",
            "result_summary": None,
        }

        status = SiblingStatus.model_validate(data)

        assert status.agent_id == "agent-456"
        assert status.sibling_index == 2
        assert status.status == "pending"
        assert status.task_summary == "Deploy application"
        assert status.result_summary is None

    def test_immutability(self) -> None:
        """Test that SiblingStatus is immutable (frozen)."""
        from pydantic import ValidationError

        status = SiblingStatus(
            agent_id="agent-123",
            sibling_index=0,
            status="pending",
            task_summary="Test task",
            result_summary=None,
        )

        with pytest.raises(ValidationError, match="frozen"):
            status.status = "completed"  # type: ignore[misc]


class TestSharedDecision:
    """Test cases for SharedDecision value object."""

    def test_create_shared_decision(self) -> None:
        """Test creating SharedDecision instance."""
        decision = SharedDecision(
            key="api_framework",
            value="FastAPI",
            rationale="Better async support",
            decided_by="agent-123",
        )

        assert decision.key == "api_framework"
        assert decision.value == "FastAPI"
        assert decision.rationale == "Better async support"
        assert decision.decided_by == "agent-123"

    def test_model_dump_serialization(self) -> None:
        """Test model_dump() serialization."""
        decision = SharedDecision(
            key="orm_framework",
            value="SQLAlchemy",
            rationale="Type hints support",
            decided_by="agent-456",
        )

        data = decision.model_dump()

        assert data["key"] == "orm_framework"
        assert data["value"] == "SQLAlchemy"
        assert data["rationale"] == "Type hints support"
        assert data["decided_by"] == "agent-456"

    def test_model_validate_deserialization(self) -> None:
        """Test model_validate() deserialization."""
        data = {
            "key": "testing_framework",
            "value": "pytest",
            "rationale": "Standard in Python",
            "decided_by": "agent-789",
        }

        decision = SharedDecision.model_validate(data)

        assert decision.key == "testing_framework"
        assert decision.value == "pytest"
        assert decision.rationale == "Standard in Python"
        assert decision.decided_by == "agent-789"


class TestSiblingView:
    """Test cases for SiblingView value object."""

    def test_create_empty_view(self) -> None:
        """Test creating view with no siblings or decisions."""
        view = SiblingView(
            current_agent_id="agent-123",
            parent_task=None,
            sibling_tasks=(),
            shared_decisions=(),
        )

        assert view.current_agent_id == "agent-123"
        assert view.parent_task is None
        assert view.sibling_tasks == ()
        assert view.shared_decisions == ()
        assert view.total_siblings == 0
        assert view.completed_count == 0
        assert view.in_progress_count == 0

    def test_create_view_with_siblings(self) -> None:
        """Test creating view with sibling tasks."""
        sibling1 = SiblingStatus(
            agent_id="sibling-1",
            sibling_index=0,
            status="completed",
            task_summary="First task",
            result_summary="Done",
        )
        sibling2 = SiblingStatus(
            agent_id="sibling-2",
            sibling_index=1,
            status="in_progress",
            task_summary="Second task",
            result_summary=None,
        )
        sibling3 = SiblingStatus(
            agent_id="sibling-3",
            sibling_index=3,
            status="pending",
            task_summary="Third task",
            result_summary=None,
        )

        view = SiblingView(
            current_agent_id="agent-current",
            parent_task="Build REST API",
            sibling_tasks=(sibling1, sibling2, sibling3),
            shared_decisions=(),
        )

        assert view.total_siblings == 3
        assert view.completed_count == 1
        assert view.in_progress_count == 1

    def test_in_progress_includes_analyzing(self) -> None:
        """Test that in_progress_count includes 'analyzing' status."""
        sibling1 = SiblingStatus(
            agent_id="sibling-1",
            sibling_index=0,
            status="analyzing",
            task_summary="Task 1",
            result_summary=None,
        )
        sibling2 = SiblingStatus(
            agent_id="sibling-2",
            sibling_index=1,
            status="in_progress",
            task_summary="Task 2",
            result_summary=None,
        )

        view = SiblingView(
            current_agent_id="agent-current",
            parent_task=None,
            sibling_tasks=(sibling1, sibling2),
            shared_decisions=(),
        )

        assert view.in_progress_count == 2

    def test_to_template_dict(self) -> None:
        """Test to_template_dict() for Jinja2 rendering."""
        sibling = SiblingStatus(
            agent_id="sibling-1",
            sibling_index=0,
            status="completed",
            task_summary="First task",
            result_summary="Done",
        )
        decision = SharedDecision(
            key="api_framework",
            value="FastAPI",
            rationale="Better async",
            decided_by="agent-123",
        )

        view = SiblingView(
            current_agent_id="agent-current",
            parent_task="Build REST API",
            sibling_tasks=(sibling,),
            shared_decisions=(decision,),
        )

        template_dict = view.to_template_dict()

        assert template_dict["current_agent_id"] == "agent-current"
        assert template_dict["parent_task"] == "Build REST API"
        assert len(template_dict["sibling_tasks"]) == 1
        assert len(template_dict["shared_decisions"]) == 1
        assert template_dict["total_siblings"] == 1
        assert template_dict["completed_count"] == 1
        assert template_dict["in_progress_count"] == 0

    def test_roundtrip_serialization(self) -> None:
        """Test model_dump() -> model_validate() roundtrip."""
        sibling = SiblingStatus(
            agent_id="sibling-1",
            sibling_index=0,
            status="completed",
            task_summary="First task",
            result_summary="Done",
        )
        decision = SharedDecision(
            key="api_framework",
            value="FastAPI",
            rationale="Better async",
            decided_by="agent-123",
        )

        original = SiblingView(
            current_agent_id="agent-current",
            parent_task="Build REST API",
            sibling_tasks=(sibling,),
            shared_decisions=(decision,),
        )

        data = original.model_dump()
        restored = SiblingView.model_validate(data)

        assert restored.current_agent_id == original.current_agent_id
        assert restored.parent_task == original.parent_task
        assert len(restored.sibling_tasks) == len(original.sibling_tasks)
        assert len(restored.shared_decisions) == len(original.shared_decisions)
        assert restored.sibling_tasks[0].agent_id == original.sibling_tasks[0].agent_id
        assert restored.shared_decisions[0].key == original.shared_decisions[0].key
