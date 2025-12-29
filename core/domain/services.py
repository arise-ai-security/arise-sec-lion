"""Domain services: parsing and utility functions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from core.domain.agent_config import AgentConfig
from core.domain.subtask import Subtask


# ---------------------------------------------------------------------------
# Value Objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredTask:
    """Value object representing a registered task for deduplication."""

    task_key: str
    task_description: str
    registered_by: UUID
    parent_id: UUID | None


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


MARKDOWN_CODE_BLOCK_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def strip_markdown_code_block(text: str) -> str:
    """Extract content from markdown code block wrapper (```json...```)."""
    match = MARKDOWN_CODE_BLOCK_PATTERN.match(text.strip())
    if match:
        return match.group(1).strip()
    return text.strip()


class SubtaskParser:
    """Parse LLM JSON responses into validated Subtask objects."""

    @staticmethod
    def parse_from_llm_response(response: str) -> list[Subtask]:
        """Parse JSON array into Subtasks. Validates config against AgentConfig schema."""
        clean_response = strip_markdown_code_block(response)

        try:
            data = json.loads(clean_response)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM response is not valid JSON: {e}") from e

        # Handle case where LLM wraps response in a dict
        if isinstance(data, dict):
            # Check if dict contains a "subtasks" or "tasks" key with a list
            for key in ("subtasks", "tasks", "items", "children"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
            else:
                # Check if it looks like a single subtask (has description or config)
                if "description" in data or "config" in data:
                    data = [data]
                else:
                    raise ValueError(
                        f"Expected list of subtasks, got dict with keys: {list(data.keys())}"
                    )

        if not isinstance(data, list):
            raise ValueError(f"Expected list of subtasks, got {type(data).__name__}")

        if len(data) == 0:
            raise ValueError("Empty subtask list")

        config_adapter = TypeAdapter(AgentConfig)
        subtasks: list[Subtask] = []

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"Subtask {idx}: expected dict, got {type(item).__name__}")

            if "config" not in item:
                raise ValueError(f"Subtask {idx}: missing 'config' field")

            try:
                config_adapter.validate_python(item["config"])
            except ValidationError as e:
                raise ValueError(f"Subtask {idx}: invalid config: {e}") from e

            try:
                subtask = Subtask(**item)
                subtasks.append(subtask)
            except ValidationError as e:
                raise ValueError(f"Subtask {idx}: validation failed: {e}") from e

        return subtasks
