"""Test cases for SubtaskParser domain service (TDD approach)."""

import json

import pytest

from core.domain.services import SubtaskParser
from core.domain.subtask import Subtask


# Helper to create standard test config
def _test_config_json():
    """Return config dict for use in JSON test data."""
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
        "tool": "claude_code",
    }


def test_parse_valid_subtask_list() -> None:
    """Test parsing valid JSON array into Subtask list."""

    # Given: Valid LLM response with subtasks (including configs)
    config = _test_config_json()
    llm_response = json.dumps(
        [
            {"description": "Research BeautifulSoup library", "config": config},
            {"description": "Implement URL fetching", "config": config},
            {"description": "Parse HTML content", "config": config},
        ]
    )

    # When: Parse the response
    subtasks = SubtaskParser.parse_from_llm_response(llm_response)

    # Then: Returns list of Subtask objects
    assert len(subtasks) == 3
    assert all(isinstance(st, Subtask) for st in subtasks)
    assert subtasks[0].description == "Research BeautifulSoup library"
    assert subtasks[1].description == "Implement URL fetching"
    assert subtasks[2].description == "Parse HTML content"


def test_parse_invalid_json_raises_value_error() -> None:
    """Test that invalid JSON raises ValueError."""

    # Given: Invalid JSON response
    llm_response = "This is not JSON at all!"

    # When/Then: Parsing raises ValueError with JSON message
    with pytest.raises(ValueError, match=r"(?i)json"):
        SubtaskParser.parse_from_llm_response(llm_response)


def test_parse_non_list_json_raises_value_error() -> None:
    """Test that non-list JSON that doesn't look like subtask raises ValueError."""

    # Given: Valid JSON but not a list and not a subtask-like dict
    llm_response = json.dumps({"foo": "bar", "baz": 123})

    # When/Then: Parsing raises ValueError with list/array message
    with pytest.raises(ValueError, match=r"(?i)(list|array)"):
        SubtaskParser.parse_from_llm_response(llm_response)


def test_parse_empty_list_raises_value_error() -> None:
    """Test that empty subtask list raises ValueError."""

    # Given: Empty JSON array
    llm_response = json.dumps([])

    # When/Then: Parsing raises ValueError with empty message
    with pytest.raises(ValueError, match=r"(?i)empty"):
        SubtaskParser.parse_from_llm_response(llm_response)


def test_parse_non_dict_item_raises_value_error() -> None:
    """Test that list containing non-dict items raises ValueError."""

    # Given: JSON array with non-dict items
    llm_response = json.dumps(["string task", 123, True])

    # When/Then: Parsing raises ValueError with structure/dict message
    with pytest.raises(ValueError, match=r"(?i)(dict|structure|object)"):
        SubtaskParser.parse_from_llm_response(llm_response)


def test_parse_missing_description_field_raises_value_error() -> None:
    """Test that subtask without description field raises ValueError."""

    # Given: JSON with object missing description
    config = _test_config_json()
    llm_response = json.dumps(
        [
            {"description": "Valid task", "config": config},
            {"priority": "high", "config": config},  # Missing description!
        ]
    )

    # When/Then: Parsing raises ValueError (from Pydantic validation)
    with pytest.raises(ValueError, match=r"(?i)(validation|description)"):
        SubtaskParser.parse_from_llm_response(llm_response)


def test_parse_empty_description_raises_value_error() -> None:
    """Test that subtask with empty description raises ValueError."""

    # Given: JSON with empty description
    config = _test_config_json()
    llm_response = json.dumps(
        [
            {"description": "", "config": config},  # Empty!
        ]
    )

    # When/Then: Parsing raises ValueError (from Pydantic min_length validation)
    with pytest.raises(ValueError, match=r"(?i)(validation|description)"):
        SubtaskParser.parse_from_llm_response(llm_response)


def test_parse_single_valid_subtask() -> None:
    """Test that single-item list is valid (though 2-3 is preferred)."""

    # Given: Single subtask (edge case, but valid)
    config = _test_config_json()
    llm_response = json.dumps(
        [
            {"description": "Complete the entire task", "config": config},
        ]
    )

    # When: Parse the response
    subtasks = SubtaskParser.parse_from_llm_response(llm_response)

    # Then: Successfully returns one subtask
    assert len(subtasks) == 1
    assert subtasks[0].description == "Complete the entire task"


def test_parse_extra_fields_ignored() -> None:
    """Test that extra fields in subtask dict are ignored."""

    # Given: JSON with extra fields beyond description and config
    config = _test_config_json()
    llm_response = json.dumps(
        [
            {
                "description": "Research libraries",
                "config": config,
                "priority": "high",
                "estimated_time": "2 hours",
            },
        ]
    )

    # When: Parse the response
    subtasks = SubtaskParser.parse_from_llm_response(llm_response)

    # Then: Successfully creates Subtask (extra fields ignored)
    assert len(subtasks) == 1
    assert subtasks[0].description == "Research libraries"


def test_parse_wrapped_subtasks_dict() -> None:
    """Test parsing when LLM wraps subtasks in a dict with 'subtasks' key."""

    # Given: LLM response wrapped in {"subtasks": [...]}
    config = _test_config_json()
    llm_response = json.dumps(
        {
            "subtasks": [
                {"description": "First task", "config": config},
                {"description": "Second task", "config": config},
            ]
        }
    )

    # When: Parse the response
    subtasks = SubtaskParser.parse_from_llm_response(llm_response)

    # Then: Successfully extracts subtasks from wrapper
    assert len(subtasks) == 2
    assert subtasks[0].description == "First task"
    assert subtasks[1].description == "Second task"


def test_parse_wrapped_tasks_dict() -> None:
    """Test parsing when LLM wraps subtasks in a dict with 'tasks' key."""

    # Given: LLM response wrapped in {"tasks": [...]}
    config = _test_config_json()
    llm_response = json.dumps(
        {
            "tasks": [
                {"description": "Only task", "config": config},
            ]
        }
    )

    # When: Parse the response
    subtasks = SubtaskParser.parse_from_llm_response(llm_response)

    # Then: Successfully extracts from 'tasks' wrapper
    assert len(subtasks) == 1
    assert subtasks[0].description == "Only task"
