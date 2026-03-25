"""Domain services: parsing and utility functions."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from core.domain.exceptions import InfeasibleError
from core.domain.values.agent_config import (
    DEFAULT_WORKER_TOOL,
    VALID_WORKER_TOOLS,
    AgentConfig,
)
from core.domain.values.constraint_failure import ConstraintFailure
from core.domain.values.subtask import Subtask


logger = logging.getLogger(__name__)


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


def parse_subtasks_from_llm(response: str) -> list[Subtask]:
    """Parse LLM response into domain objects.

    Raises:
        InfeasibleError: If LLM indicates constraints cannot be satisfied.
        ValueError: If the response cannot be parsed into valid subtasks.
    """
    data = _parse_json(response)

    if ConstraintFailure.matches(data):
        raise InfeasibleError(ConstraintFailure.from_llm_response(data))

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
            raise ValueError(f"Expected list of subtasks, got dict with keys: {list(d.keys())}")
        case _:
            raise ValueError(f"Expected list of subtasks, got {type(data).__name__}")


def _sanitize_llm_subtask(item: dict[str, Any], idx: int) -> dict[str, Any]:
    """Normalize LLM-hallucinated values in a subtask dict.

    Fixes known hallucination patterns (invalid tool names, non-integer
    depends_on) so downstream validation sees clean data.  Logs warnings
    when corrections are applied so regressions are observable.
    """
    cfg = item.get("config")
    if isinstance(cfg, dict):
        tool = cfg.get("tool")
        if tool is not None and tool not in VALID_WORKER_TOOLS:
            logger.warning(
                "Subtask %d: sanitized invalid tool %r → %r",
                idx,
                tool,
                DEFAULT_WORKER_TOOL,
            )
            cfg["tool"] = DEFAULT_WORKER_TOOL

    if "depends_on" in item:
        raw = item["depends_on"]
        clean: list[int] = []
        dropped: list[Any] = []
        for v in raw:
            if isinstance(v, int):
                clean.append(v)
            elif isinstance(v, str) and v.isdigit():
                clean.append(int(v))
            else:
                dropped.append(v)
        if dropped:
            logger.warning(
                "Subtask %d: dropped non-integer depends_on values: %r",
                idx,
                dropped,
            )
        item["depends_on"] = clean

    return item


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

        sanitized_item = _sanitize_llm_subtask(item, idx)

        try:
            config_adapter.validate_python(sanitized_item["config"])
        except ValidationError as e:
            raise ValueError(f"Subtask {idx}: invalid config: {e}") from e

        try:
            subtasks.append(Subtask(**sanitized_item))
        except ValidationError as e:
            raise ValueError(f"Subtask {idx}: validation failed: {e}") from e

    return subtasks


# ---------------------------------------------------------------------------
# Assessment parsing (unified evaluate+decompose response)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """Result of task assessment: execute, decompose, or infeasible."""

    action: Literal["execute", "decompose", "infeasible"]
    reasoning: str = ""
    subtasks: list[Subtask] | None = None
    constraint_failure: ConstraintFailure | None = None


def parse_assessment_response(response: str) -> AssessmentResult:
    """Parse LLM assessment response (execute/decompose union).

    Returns:
        AssessmentResult with action, reasoning, and optional subtasks.

    Raises:
        json.JSONDecodeError: If response is not valid JSON.
        ValueError: If action is invalid or subtasks are malformed.
    """
    clean = strip_markdown_code_block(response)
    data = json.loads(clean)

    # Check constraint failure first
    if ConstraintFailure.matches(data):
        return AssessmentResult(
            action="infeasible",
            constraint_failure=ConstraintFailure.from_llm_response(data),
        )

    action = data.get("action", "").lower()
    reasoning = data.get("reasoning", "")

    if action == "execute":
        return AssessmentResult(action="execute", reasoning=reasoning)

    if action == "decompose":
        raw_subtasks = data.get("subtasks", [])
        subtasks = _validate_subtasks(raw_subtasks)
        return AssessmentResult(
            action="decompose",
            reasoning=reasoning,
            subtasks=subtasks,
        )

    raise ValueError(f"Invalid action: '{action}'. Expected 'execute' or 'decompose'.")
