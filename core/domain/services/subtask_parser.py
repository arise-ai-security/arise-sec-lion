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
        case {"name": _, "arguments": dict() as args}:
            # Qwen-family models (e.g. qwen3.5 via Ollama) emit tool-call-shaped
            # JSON {"name": "...", "arguments": {...}} even when no tools are
            # offered.  Unwrap and recurse into the arguments payload.
            return _extract_subtask_list(args)
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

    justification = item.get("justification")
    if isinstance(justification, dict):
        coerced = False
        for k, v in justification.items():
            if not isinstance(v, str):
                justification[k] = json.dumps(v)
                coerced = True
        if coerced:
            logger.warning("Subtask %d: coerced non-string justification values to JSON", idx)

    if "depends_on" in item:
        raw = item["depends_on"]
        clean: list[int] = []
        deferred_strings: list[str] = []
        for v in raw:
            if isinstance(v, int):
                clean.append(v)
            elif isinstance(v, str) and v.isdigit():
                clean.append(int(v))
            elif isinstance(v, str):
                deferred_strings.append(v)
            else:
                logger.warning("Subtask %d: dropped non-string/int depends_on: %r", idx, v)
        # Store string references for cross-subtask resolution in _resolve_depends_on
        item["depends_on"] = clean
        if deferred_strings:
            item["_deferred_depends_on"] = deferred_strings

    return item


# Pattern to extract bracketed prefix from description, e.g. "[Build-Setup]"
_BRACKET_PREFIX = re.compile(r"^\[([^\]]+)\]")


def _resolve_depends_on(items: list[dict[str, Any]]) -> None:
    """Resolve string task-name references in depends_on to integer indices.

    LLMs sometimes produce depends_on: ["Build-Setup"] instead of [0].
    Build a name→index map from description prefixes and resolve them.
    """
    # Build name→index map from description bracketed prefixes
    name_to_idx: dict[str, int] = {}
    for idx, item in enumerate(items):
        desc = item.get("description", "")
        m = _BRACKET_PREFIX.match(desc)
        if m:
            name_to_idx[m.group(1)] = idx

    # Resolve deferred string references
    for idx, item in enumerate(items):
        deferred = item.pop("_deferred_depends_on", None)
        if not deferred:
            continue

        clean = list(item.get("depends_on", []))
        dropped: list[str] = []
        for name in deferred:
            # Try exact match, then strip brackets (LLM may include them)
            stripped = name.strip("[]")
            resolved_idx = name_to_idx.get(name) or name_to_idx.get(stripped)
            if resolved_idx is not None:
                clean.append(resolved_idx)
                logger.info(
                    "Subtask %d: resolved depends_on %r → index %d",
                    idx, name, resolved_idx,
                )
            else:
                dropped.append(name)
        if dropped:
            logger.warning(
                "Subtask %d: dropped unresolvable depends_on names: %r",
                idx, dropped,
            )
        item["depends_on"] = clean


def _validate_subtasks(items: list[dict[str, Any]]) -> list[Subtask]:
    """Validate and create Subtask objects from dicts."""
    if not items:
        raise ValueError("Empty subtask list")

    # Pre-sanitize all items so name→index map is complete
    for idx, item in enumerate(items):
        if isinstance(item, dict):
            _sanitize_llm_subtask(item, idx)

    # Resolve string depends_on references across all subtasks
    _resolve_depends_on(items)

    config_adapter = TypeAdapter(AgentConfig)
    subtasks: list[Subtask] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Subtask {idx}: expected dict, got {type(item).__name__}")

        if "config" not in item:
            # Qwen-family models sometimes omit the config field entirely.
            # Inject a safe default so downstream validation succeeds.
            logger.warning("Subtask %d: missing 'config' field, injecting default", idx)
            item["config"] = {
                "strategy": "heuristic",
                "base": {"model": "placeholder", "temperature": 0.7, "max_tokens": 16000},
                "tool": DEFAULT_WORKER_TOOL,
            }

        sanitized_item = item  # Already sanitized above

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

    # Qwen-family models may wrap the response in a tool-call shape;
    # unwrap {"name": ..., "arguments": {...}} to get the real payload.
    if isinstance(data, dict) and "name" in data and "arguments" in data:
        inner = data["arguments"]
        if isinstance(inner, dict):
            logger.warning("Assessment response wrapped in tool-call shape, unwrapping")
            data = inner

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

    # Qwen-family models sometimes omit the "action" field entirely.
    # Infer intent from response structure: if subtasks are present,
    # treat as decompose; otherwise default to execute.
    if not action:
        raw_subtasks = data.get("subtasks") or data.get("tasks") or data.get("children")
        if raw_subtasks and isinstance(raw_subtasks, list):
            logger.warning("Assessment missing 'action' field but has subtasks, inferring 'decompose'")
            subtasks = _validate_subtasks(raw_subtasks)
            return AssessmentResult(
                action="decompose",
                reasoning=reasoning,
                subtasks=subtasks,
            )
        # No subtasks and no action — default to execute (single worker)
        logger.warning("Assessment missing 'action' field and no subtasks, defaulting to 'execute'")
        return AssessmentResult(action="execute", reasoning=reasoning)

    raise ValueError(f"Invalid action: '{action}'. Expected 'execute' or 'decompose'.")
