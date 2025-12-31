"""Domain services: parsing and utility functions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from core.domain.agent_config import AgentConfig
from core.domain.subtask import Subtask


# ---------------------------------------------------------------------------
# Value Objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisteredTask:
    """Value object representing a registered task for deduplication."""

    task_key: str
    task_description: str
    registered_by: UUID
    parent_id: UUID | None


@dataclass(frozen=True, slots=True)
class ConstraintFailure:
    """Value object for when LLM cannot satisfy execution constraints.

    Returned when LLM responds with constraints_unsatisfiable instead of
    a valid subtask list. This allows graceful failure rather than
    spawning doomed workers.
    """

    reason: str
    minimum_subtasks: int | None = None
    minimum_depth: int | None = None

    @classmethod
    def matches(cls, data: Any) -> bool:
        """Check if data represents a constraint failure response."""
        return isinstance(data, dict) and data.get("status") == "constraints_unsatisfiable"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConstraintFailure:
        """Create from LLM response dict."""
        minimum_required = data.get("minimum_required", {})
        return cls(
            reason=data.get("reason", "Unknown reason"),
            minimum_subtasks=minimum_required.get("subtasks"),
            minimum_depth=minimum_required.get("depth_levels"),
        )

    def format_message(self) -> str:
        """Format failure as human-readable message."""
        msg = f"Constraints unsatisfiable: {self.reason}"
        if self.minimum_subtasks is not None:
            msg += f" (needs at least {self.minimum_subtasks} subtasks)"
        if self.minimum_depth is not None:
            msg += f" (needs {self.minimum_depth} more depth levels)"
        return msg


# ---------------------------------------------------------------------------
# Domain Services
# ---------------------------------------------------------------------------


class TaskKeyGenerator:
    """Generate normalized task keys for deduplication.

    Uses SHA256 hash of normalized description to create a compact,
    deterministic key for task deduplication.
    """

    @staticmethod
    def generate_key(description: str) -> str:
        """Generate normalized key from task description.

        Args:
            description: The task description to hash.

        Returns:
            16-character hex string (first 64 bits of SHA256).
        """
        # Normalize: lowercase, strip, collapse whitespace
        normalized = description.lower().strip()
        normalized = re.sub(r"\s+", " ", normalized)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# Patterns for extracting JSON from LLM responses
# Pattern 1: Full match - entire response is wrapped in code block
_FULL_CODE_BLOCK_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)
# Pattern 2: Search - code block anywhere in response (handles extra text after)
_SEARCH_CODE_BLOCK_PATTERN = re.compile(
    r"```(?:json)?\s*\n(.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE,
)


def strip_markdown_code_block(text: str) -> str:
    """Extract content from markdown code block wrapper (```json...```).

    Handles cases where LLM adds explanatory text before or after the JSON block.
    """
    stripped = text.strip()

    # First try exact match (entire response is the code block)
    match = _FULL_CODE_BLOCK_PATTERN.match(stripped)
    if match:
        return match.group(1).strip()

    # Fall back to search (code block somewhere in response)
    match = _SEARCH_CODE_BLOCK_PATTERN.search(stripped)
    if match:
        return match.group(1).strip()

    return stripped


def parse_subtasks_from_llm(response: str) -> list[Subtask] | ConstraintFailure:
    """Parse LLM response into domain objects.

    Returns ConstraintFailure if LLM indicates constraints cannot be satisfied.
    Returns list of validated Subtask objects otherwise.
    """
    data = _parse_json(response)

    # Objects recognize themselves - Tell, Don't Ask
    if ConstraintFailure.matches(data):
        return ConstraintFailure.from_dict(data)

    items = _extract_subtask_list(data)
    return _validate_subtasks(items)


def _parse_json(response: str) -> Any:
    """Extract and parse JSON from LLM response."""
    clean = strip_markdown_code_block(response)
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM response is not valid JSON: {e}") from e


def _extract_subtask_list(data: Any) -> list[dict[str, Any]]:
    """Normalize various LLM response formats to subtask list."""
    match data:
        case list() as items:
            return items
        case {"subtasks": list() as items}:
            return items
        case {"tasks": list() as items}:
            return items
        case {"items": list() as items}:
            return items
        case {"children": list() as items}:
            return items
        case {"description": _} | {"config": _}:
            return [data]  # Single subtask wrapped
        case dict() as d:
            raise ValueError(
                f"Expected list of subtasks, got dict with keys: {list(d.keys())}"
            )
        case _:
            raise ValueError(f"Expected list of subtasks, got {type(data).__name__}")


def _validate_subtasks(items: list[dict[str, Any]]) -> list[Subtask]:
    """Validate and create Subtask objects from dicts."""
    if not items:
        raise ValueError("Empty subtask list")

    config_adapter = TypeAdapter(AgentConfig)
    subtasks: list[Subtask] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Subtask {idx}: expected dict, got {type(item).__name__}")

        if "config" not in item:
            raise ValueError(f"Subtask {idx}: missing 'config' field")

        try:
            config_adapter.validate_python(item["config"])
        except ValidationError as e:
            raise ValueError(f"Subtask {idx}: invalid config: {e}") from e

        try:
            subtasks.append(Subtask(**item))
        except ValidationError as e:
            raise ValueError(f"Subtask {idx}: validation failed: {e}") from e

    return subtasks
