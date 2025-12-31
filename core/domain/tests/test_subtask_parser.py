"""Test cases for subtask parsing domain service (TDD approach)."""

import json

import pytest

from core.domain.services import ConstraintFailure, parse_subtasks_from_llm
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
    subtasks = parse_subtasks_from_llm(llm_response)

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
        parse_subtasks_from_llm(llm_response)


def test_parse_non_list_json_raises_value_error() -> None:
    """Test that non-list JSON that doesn't look like subtask raises ValueError."""

    # Given: Valid JSON but not a list and not a subtask-like dict
    llm_response = json.dumps({"foo": "bar", "baz": 123})

    # When/Then: Parsing raises ValueError with list/array message
    with pytest.raises(ValueError, match=r"(?i)(list|array)"):
        parse_subtasks_from_llm(llm_response)


def test_parse_empty_list_raises_value_error() -> None:
    """Test that empty subtask list raises ValueError."""

    # Given: Empty JSON array
    llm_response = json.dumps([])

    # When/Then: Parsing raises ValueError with empty message
    with pytest.raises(ValueError, match=r"(?i)empty"):
        parse_subtasks_from_llm(llm_response)


def test_parse_non_dict_item_raises_value_error() -> None:
    """Test that list containing non-dict items raises ValueError."""

    # Given: JSON array with non-dict items
    llm_response = json.dumps(["string task", 123, True])

    # When/Then: Parsing raises ValueError with structure/dict message
    with pytest.raises(ValueError, match=r"(?i)(dict|structure|object)"):
        parse_subtasks_from_llm(llm_response)


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
        parse_subtasks_from_llm(llm_response)


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
        parse_subtasks_from_llm(llm_response)


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
    subtasks = parse_subtasks_from_llm(llm_response)

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
    subtasks = parse_subtasks_from_llm(llm_response)

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
    subtasks = parse_subtasks_from_llm(llm_response)

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
    subtasks = parse_subtasks_from_llm(llm_response)

    # Then: Successfully extracts from 'tasks' wrapper
    assert len(subtasks) == 1
    assert subtasks[0].description == "Only task"


# ---------------------------------------------------------------------------
# ConstraintFailure value object tests
# ---------------------------------------------------------------------------


def test_constraint_failure_matches_valid_data() -> None:
    """Test that ConstraintFailure.matches() recognizes valid constraint failure data."""
    # Given: Data with constraints_unsatisfiable status
    data = {
        "status": "constraints_unsatisfiable",
        "reason": "Task requires more depth",
    }

    # When/Then: matches() returns True
    assert ConstraintFailure.matches(data) is True


def test_constraint_failure_matches_rejects_other_dicts() -> None:
    """Test that ConstraintFailure.matches() rejects non-failure dicts."""
    # Given: Various non-failure data
    test_cases = [
        {"status": "success"},
        {"subtasks": []},
        {"description": "A task"},
        {},
        "not a dict",
        None,
        42,
    ]

    # When/Then: matches() returns False for all
    for data in test_cases:
        assert ConstraintFailure.matches(data) is False


def test_constraint_failure_from_dict() -> None:
    """Test that ConstraintFailure.from_dict() creates correct object."""
    # Given: Full constraint failure response
    data = {
        "status": "constraints_unsatisfiable",
        "reason": "Cannot split further",
        "minimum_required": {
            "subtasks": 3,
            "depth_levels": 2,
        },
    }

    # When: Create from dict
    failure = ConstraintFailure.from_dict(data)

    # Then: All fields populated correctly
    assert failure.reason == "Cannot split further"
    assert failure.minimum_subtasks == 3
    assert failure.minimum_depth == 2


def test_constraint_failure_from_dict_with_defaults() -> None:
    """Test that ConstraintFailure.from_dict() handles missing fields."""
    # Given: Minimal constraint failure response
    data = {"status": "constraints_unsatisfiable"}

    # When: Create from dict
    failure = ConstraintFailure.from_dict(data)

    # Then: Defaults applied
    assert failure.reason == "Unknown reason"
    assert failure.minimum_subtasks is None
    assert failure.minimum_depth is None


def test_constraint_failure_format_message_full() -> None:
    """Test format_message() with all fields populated."""
    # Given: ConstraintFailure with all fields
    failure = ConstraintFailure(
        reason="Task too atomic",
        minimum_subtasks=2,
        minimum_depth=1,
    )

    # When: Format message
    msg = failure.format_message()

    # Then: Message includes all details
    assert "Constraints unsatisfiable: Task too atomic" in msg
    assert "(needs at least 2 subtasks)" in msg
    assert "(needs 1 more depth levels)" in msg


def test_constraint_failure_format_message_minimal() -> None:
    """Test format_message() with only reason."""
    # Given: ConstraintFailure with only reason
    failure = ConstraintFailure(reason="Cannot decompose")

    # When: Format message
    msg = failure.format_message()

    # Then: Message has reason only
    assert msg == "Constraints unsatisfiable: Cannot decompose"


def test_parse_constraint_failure_response() -> None:
    """Test that parse_subtasks_from_llm returns ConstraintFailure for unsatisfiable response."""
    # Given: LLM constraint failure response
    llm_response = json.dumps(
        {
            "status": "constraints_unsatisfiable",
            "reason": "Depth limit reached",
            "minimum_required": {"subtasks": 2},
        }
    )

    # When: Parse response
    result = parse_subtasks_from_llm(llm_response)

    # Then: Returns ConstraintFailure, not a list
    assert isinstance(result, ConstraintFailure)
    assert result.reason == "Depth limit reached"
    assert result.minimum_subtasks == 2
