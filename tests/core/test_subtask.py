"""Test cases for Subtask value object (TDD approach)."""

import pytest
from pydantic import ValidationError

from core.domain.subtask import Subtask


def test_create_valid_subtask() -> None:
    """Test that a valid subtask can be created."""

    # Given: Valid subtask description
    description = "Research BeautifulSoup library"

    # When: Create a Subtask
    subtask = Subtask(description=description)

    # Then: Subtask is created with correct description
    assert subtask.description == description


def test_subtask_is_immutable() -> None:
    """Test that Subtask is immutable (frozen)."""

    # Given: A created subtask
    subtask = Subtask(description="Implement URL fetching")

    # When/Then: Attempting to modify raises error
    with pytest.raises((ValidationError, AttributeError)):
        subtask.description = "Modified description"  # type: ignore


def test_subtask_rejects_empty_description() -> None:
    """Test that Subtask rejects empty description."""

    # Given: Empty description
    # When: Attempt to create Subtask with empty description
    # Then: ValidationError is raised
    with pytest.raises(ValidationError) as exc_info:
        Subtask(description="")

    # And: Error message mentions description
    assert "description" in str(exc_info.value).lower()


def test_subtask_requires_description_field() -> None:
    """Test that description field is required."""

    # Given: No description provided
    # When/Then: Creating Subtask without description raises ValidationError
    with pytest.raises(ValidationError) as exc_info:
        Subtask()  # type: ignore

    # And: Error mentions missing field
    assert "description" in str(exc_info.value).lower()


def test_subtask_equality() -> None:
    """Test that Subtasks with same description are equal (value object)."""

    # Given: Two subtasks with identical descriptions
    subtask1 = Subtask(description="Parse HTML content")
    subtask2 = Subtask(description="Parse HTML content")

    # Then: They should be equal (value object semantics)
    assert subtask1 == subtask2


def test_subtask_inequality() -> None:
    """Test that Subtasks with different descriptions are not equal."""

    # Given: Two subtasks with different descriptions
    subtask1 = Subtask(description="Parse HTML content")
    subtask2 = Subtask(description="Extract links from page")

    # Then: They should not be equal
    assert subtask1 != subtask2


def test_subtask_hashable() -> None:
    """Test that Subtask can be used in sets/dicts (frozen)."""

    # Given: Multiple subtasks
    subtask1 = Subtask(description="Task A")
    subtask2 = Subtask(description="Task B")
    subtask3 = Subtask(description="Task A")  # Duplicate

    # When: Add to set
    subtask_set = {subtask1, subtask2, subtask3}

    # Then: Set contains only unique subtasks
    assert len(subtask_set) == 2
    assert subtask1 in subtask_set
    assert subtask2 in subtask_set
