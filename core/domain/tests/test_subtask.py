"""Test cases for Subtask value object (TDD approach)."""

import pytest
from pydantic import ValidationError

from core.domain.values.subtask import Subtask


# Helper function to create standard test config
def _test_config():
    """Create a standard test config for subtasks."""
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }


def test_create_valid_subtask() -> None:
    """Test that a valid subtask can be created."""

    # Given: Valid subtask description and config
    description = "Research BeautifulSoup library"
    config = _test_config()

    # When: Create a Subtask
    subtask = Subtask(description=description, config=config)

    # Then: Subtask is created with correct description and config
    assert subtask.description == description
    assert subtask.config == config


def test_subtask_is_immutable() -> None:
    """Test that Subtask is immutable (frozen)."""

    # Given: A created subtask
    subtask = Subtask(description="Implement URL fetching", config=_test_config())

    # When/Then: Attempting to modify raises error
    with pytest.raises((ValidationError, AttributeError)):
        subtask.description = "Modified description"  # type: ignore


def test_subtask_rejects_empty_description() -> None:
    """Test that Subtask rejects empty description."""

    # Given: Empty description
    # When: Attempt to create Subtask with empty description
    # Then: ValidationError is raised
    with pytest.raises(ValidationError) as exc_info:
        Subtask(description="", config=_test_config())

    # And: Error message mentions description
    assert "description" in str(exc_info.value).lower()


def test_subtask_requires_description_field() -> None:
    """Test that description field is required."""

    # Given: No description provided
    # When/Then: Creating Subtask without description raises ValidationError
    with pytest.raises(ValidationError) as exc_info:
        Subtask(config=_test_config())  # type: ignore

    # And: Error mentions missing field
    assert "description" in str(exc_info.value).lower()


def test_subtask_equality() -> None:
    """Test that Subtasks with same description and config are equal (value object)."""

    # Given: Two subtasks with identical descriptions and configs
    config = _test_config()
    subtask1 = Subtask(description="Parse HTML content", config=config)
    subtask2 = Subtask(description="Parse HTML content", config=config)

    # Then: They should be equal (value object semantics)
    assert subtask1 == subtask2


def test_subtask_inequality() -> None:
    """Test that Subtasks with different descriptions are not equal."""

    # Given: Two subtasks with different descriptions
    config = _test_config()
    subtask1 = Subtask(description="Parse HTML content", config=config)
    subtask2 = Subtask(description="Extract links from page", config=config)

    # Then: They should not be equal
    assert subtask1 != subtask2


def test_subtask_defaults_for_new_fields() -> None:
    """Test that new enriched fields have sensible defaults."""
    subtask = Subtask(description="Do thing", config=_test_config())

    assert subtask.depends_on == []
    assert subtask.dependency_type == "finish_to_start"
    assert subtask.estimated_complexity == "unknown"
    assert subtask.success_criteria == ""
    assert subtask.failure_indicators == []
    assert subtask.task_type == "general"


def test_subtask_with_enriched_fields() -> None:
    """Test that enriched fields can be set from LLM response dicts."""
    subtask = Subtask(
        description="Implement auth module",
        config=_test_config(),
        depends_on=[0],
        estimated_complexity="complex",
        success_criteria="All auth endpoints return 200",
        failure_indicators=["compilation error", "test failure"],
        task_type="implementation",
    )

    assert subtask.depends_on == [0]
    assert subtask.estimated_complexity == "complex"
    assert subtask.success_criteria == "All auth endpoints return 200"
    assert subtask.failure_indicators == ["compilation error", "test failure"]
    assert subtask.task_type == "implementation"


def test_subtask_from_dict_with_extra_fields_ignored() -> None:
    """Test that unknown fields from LLM are ignored (Pydantic default)."""
    data = {
        "description": "Task",
        "config": _test_config(),
        "justification": {"objective": "x", "plan": "y"},  # LLM adds this
        "unknown_field": "should be ignored",
    }
    subtask = Subtask(**data)
    assert subtask.description == "Task"


def test_subtask_not_hashable_due_to_config() -> None:
    """Test that Subtask is not hashable due to dict config field.

    Note: With the addition of the config field (which is a dict),
    Subtasks are no longer hashable. This is acceptable - many value
    objects with complex nested structures are not hashable.

    Subtasks can still be compared for equality and are immutable (frozen).
    """

    # Given: Multiple subtasks
    config = _test_config()
    subtask1 = Subtask(description="Task A", config=config)

    # When/Then: Attempting to use in set raises TypeError
    with pytest.raises(TypeError, match=r"(?i)unhashable"):
        _ = {subtask1}  # Sets require hashable elements
