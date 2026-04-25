"""Domain services: parsing and utility functions."""

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import TypeAdapter, ValidationError

from core.domain.exceptions import InfeasibleError
from core.domain.values.agent_config import (
    DEFAULT_WORKER_TOOL,
    VALID_WORKER_TOOLS,
    AgentConfig,
)
from core.domain.values.constraint_failure import ConstraintFailure
from core.domain.values.subtask import Subtask


if TYPE_CHECKING:
    from core.ports.runtime_ports import FormatRepairerPort


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema hints for the LLM-backed format repairer (FormatRepairerPort).
# ---------------------------------------------------------------------------
# We derive the per-object schemas from Pydantic models at runtime so
# that any new required field, renamed field, or removed field on
# Subtask / AgentConfig / ConstraintFailure automatically propagates
# into the repairer prompt. The outer envelope (action/reasoning/
# subtasks) is hand-parsed and kept as a small template that
# interpolates the dynamic blocks.

from functools import lru_cache  # noqa: E402  — placed near the schema helpers


def _compact_pydantic_schema(model_class: Any) -> str:
    """Render a Pydantic model's JSON schema as a compact JSON string.

    The schema includes ``$defs``, ``required``, ``properties.{name.type}``,
    enum values, etc. — i.e. every constraint downstream validation will
    enforce. The repairer prompt embeds this verbatim so the model has
    the exact shape the parser expects.
    """
    return json.dumps(model_class.model_json_schema(), indent=2, sort_keys=True)


@lru_cache(maxsize=1)
def build_assessment_schema_hint() -> str:
    """Build the assessment-response schema hint from current Pydantic models.

    Cached because Pydantic schemas do not change at runtime; if a model
    is hot-reloaded during dev, restart the process to pick up changes.
    """
    subtask_schema = _compact_pydantic_schema(Subtask)
    failure_schema = _compact_pydantic_schema(ConstraintFailure)
    return f"""\
A JSON object representing one of three actions from a task-assessment
agent. Pick the action that matches the original (malformed) output.

Action 1 — "execute" (single-worker task):
  {{
    "action": "execute",
    "reasoning": "<why a single worker suffices, preserved verbatim>"
  }}

Action 2 — "decompose" (split into child subtasks):
  {{
    "action": "decompose",
    "reasoning": "<why decomposition is needed>",
    "subtasks": [<Subtask>, <Subtask>, ...]
  }}

  Each <Subtask> object follows this JSON Schema (auto-generated from
  the downstream Pydantic model — every field marked "required" must
  be present if the original payload contained it):

  {subtask_schema}

Action 3 — constraint failure (parser tolerates this in place of action):
  This object follows the ConstraintFailure schema:

  {failure_schema}

  In raw form it looks like:
  {{
    "status": "constraints_unsatisfiable",
    "reason": "<why>",
    "minimum_required": {{"subtasks": <int>, "depth_levels": <int>}}
  }}

Preserve every value verbatim. Omit any field the original did not
include — the parser will fail with a clear validation error if a
required field is missing, which is the correct behavior."""


@lru_cache(maxsize=1)
def build_subtasks_schema_hint() -> str:
    """Build the subtasks-list schema hint from current Pydantic models."""
    subtask_schema = _compact_pydantic_schema(Subtask)
    failure_schema = _compact_pydantic_schema(ConstraintFailure)
    return f"""\
A JSON array of subtask objects, or an object whose "subtasks" /
"tasks" / "items" / "children" key holds that array. Preserve whichever
shape the original used.

Each subtask object follows this JSON Schema (auto-generated from the
downstream Pydantic model):

{subtask_schema}

If the original payload was a constraint-failure marker rather than a
subtask list, return that instead. Constraint failure schema:

{failure_schema}

In raw form a constraint failure looks like:
{{
  "status": "constraints_unsatisfiable",
  "reason": "<why>",
  "minimum_required": {{"subtasks": <int>, "depth_levels": <int>}}
}}

Preserve every value verbatim. Omit any field the original did not
include."""


# Stable string names for direct use by callers. Resolved at import
# time via lru_cache; call ``build_*_schema_hint.cache_clear()`` and
# re-import the constants if Pydantic models are hot-swapped during
# testing. Call sites pass these directly to
# ``FormatRepairerPort.repair(raw, schema_hint)``.
ASSESSMENT_SCHEMA_HINT: str = build_assessment_schema_hint()
SUBTASKS_SCHEMA_HINT: str = build_subtasks_schema_hint()


# ---------------------------------------------------------------------------
# Standard JSON extraction (GPT / Claude)
# ---------------------------------------------------------------------------
# GPT and Claude reliably return a single JSON object, optionally wrapped in
# a ```json … ``` markdown code block.  The two patterns below handle the
# code-block case; plain JSON passes through unchanged.

_FULL_CODE_BLOCK_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_SEARCH_CODE_BLOCK_PATTERN = re.compile(
    r"```(?:json)?\s*\n(.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Ollama / Qwen-family response sanitisation
# ---------------------------------------------------------------------------
# Qwen models served through Ollama (qwen3, qwen3-coder, qwen3.5) deviate
# from the GPT/Claude output contract in several ways that Ollama itself
# does NOT repair — Ollama's own Qwen3 parser hard-crashes (HTTP 500) on
# malformed JSON (ollama/ollama#14570; fix pending in PR #14835).
#
# We handle the following Qwen-specific behaviors here so that the rest of
# the codebase sees clean JSON identical to what GPT/Claude produce.
#
# 1. <think>…</think> tags — Qwen wraps chain-of-thought reasoning in
#    these tags.  Three variants observed:
#      a. "<think>…</think>\n{JSON}" — tags precede the answer.
#      b. "<think>…</think>"         — entire response is thinking only;
#         Ollama sets content="" (ollama/ollama#10976).
#      c. "{JSON}\n<think>…</think>" — tags trail the answer.
#    GPT / Claude never emit <think> tags — stripping is a no-op for them.
#
# 2. Trailing content after JSON — extra JSON objects, prose, or think
#    tags after the primary JSON value.  Handled by raw_decode (parses
#    only the first JSON value, ignores the rest).
#
# 3. Structurally invalid JSON — trailing commas, unclosed strings/braces,
#    response truncated by num_predict or context-window limits.
#    Handled by _repair_json (lightweight Gemma4-style repair).

_THINK_TAG_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_markdown_code_block(text: str) -> str:
    """Extract content from markdown code block wrapper (```json...```).

    Also strips Qwen-family ``<think>…</think>`` reasoning blocks that
    precede, follow, or surround the JSON payload.  Thinking tags are
    removed first so that code-block extraction sees clean content.

    No-op for GPT/Claude (they never emit ``<think>`` tags or require
    code-block unwrapping beyond what they already produce).
    """
    # Strip <think>…</think> blocks (Qwen thinking mode).
    stripped = _THINK_TAG_PATTERN.sub("", text).strip()

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


def _repair_json(text: str) -> str | None:
    """Attempt lightweight repair of malformed JSON from Qwen-family models.

    Qwen models (qwen3, qwen3-coder via Ollama) produce structurally
    invalid JSON in large decomposition responses.  Observed patterns:

      1. Trailing commas before ``}`` or ``]``  (``{..., }``).
      2. Unclosed strings / objects — the response was truncated by
         ``num_predict`` or context-window limits (ollama/ollama#14570).
      3. Single-quoted string values instead of double-quoted.

    Inspired by Ollama's Gemma4 parser (``repairGemma4ToolCallArgs``),
    which is the only Ollama parser that attempts JSON repair.  Ollama's
    own Qwen3 parser does **not** repair — it returns HTTP 500 on
    malformed JSON (fix pending in PR #14835).

    GPT / Claude never produce malformed JSON, so this is a no-op path
    that is only reached after a ``JSONDecodeError`` from ``raw_decode``.

    Returns repaired text, or ``None`` if repair was not possible.
    """
    candidate = text

    # Fix 1: trailing commas  {…, }  or  […, ]
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)

    # Fix 2: single-quoted strings → double-quoted
    # Only apply if the text has no double quotes at all (avoids
    # breaking strings that legitimately contain single quotes).
    if '"' not in candidate and "'" in candidate:
        candidate = candidate.replace("'", '"')

    # Fix 3: unclosed objects / arrays — try closing them
    opens = candidate.count("{") - candidate.count("}")
    candidate += "}" * max(opens, 0)
    opens_arr = candidate.count("[") - candidate.count("]")
    candidate += "]" * max(opens_arr, 0)

    # Fix 4: unclosed string at end — close it, then close containers
    if candidate.count('"') % 2 != 0:
        candidate += '"'
        # Re-balance after adding the closing quote
        opens = candidate.count("{") - candidate.count("}")
        candidate += "}" * max(opens, 0)
        opens_arr = candidate.count("[") - candidate.count("]")
        candidate += "]" * max(opens_arr, 0)

    if candidate == text:
        return None  # no repairs attempted

    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        return None


def _parse_json(response: str) -> Any:
    """Extract and parse JSON from LLM response.

    Two-tier strategy to handle both GPT/Claude and Ollama/Qwen models:

    1. **Standard path (GPT / Claude)** — ``raw_decode`` on the cleaned
       text.  Succeeds on the first try for well-formed JSON; also
       tolerates trailing content that Qwen may append after the object.
    2. **Ollama / Qwen repair fallback** — only reached when raw_decode
       raises ``JSONDecodeError``.  Applies lightweight structural fixes
       (trailing commas, unclosed braces/strings) inspired by Ollama's
       Gemma4 parser.  Never reached for GPT/Claude.
    """
    clean = strip_markdown_code_block(response)

    # --- Standard path (GPT / Claude — always succeeds here) -----------
    try:
        # lstrip() is defensive: raw_decode (unlike json.loads) rejects
        # leading whitespace.  strip_markdown_code_block already strips,
        # but we guard against future changes to that function.
        data, _ = json.JSONDecoder().raw_decode(clean.lstrip())
        return data
    except json.JSONDecodeError:
        pass

    # --- Ollama / Qwen repair fallback ---------------------------------
    repaired = _repair_json(clean)
    if repaired is not None:
        logger.warning("Repaired malformed JSON from LLM (Qwen quirk)")
        return json.loads(repaired)

    raise ValueError(f"LLM response is not valid JSON and repair failed: {clean[:200]}")


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
        if not isinstance(item, dict):
            continue
        desc = item.get("description", "")
        m = _BRACKET_PREFIX.match(desc)
        if m:
            name_to_idx[m.group(1)] = idx

    # Resolve deferred string references
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
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
    logger.debug("Assessment response (first 500 chars): %.500s", clean)

    # --- Standard path (GPT / Claude — always succeeds here) -----------
    clean_lstripped = clean.lstrip()
    try:
        data, end = json.JSONDecoder().raw_decode(clean_lstripped)
    except json.JSONDecodeError:
        # --- Ollama / Qwen repair fallback -----------------------------
        # Qwen produces structurally invalid JSON (trailing commas,
        # unclosed braces, unescaped quotes in large responses).
        # Ollama does not repair this (ollama/ollama#14570).
        repaired = _repair_json(clean_lstripped)
        if repaired is None:
            raise
        logger.warning("Repaired malformed assessment JSON (Qwen quirk)")
        data = json.loads(repaired)
        end = len(repaired)

    # Qwen may append trailing content (think tags, prose, duplicate JSON)
    # after the primary object.  GPT/Claude never do this.
    trailing = clean_lstripped[end:].strip()
    if trailing:
        logger.warning(
            "Assessment response had trailing content after JSON "
            "(Qwen quirk), ignored %d chars: %.120s",
            len(trailing),
            trailing,
        )

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


# ---------------------------------------------------------------------------
# Async variants with LLM-backed format-repair fallback
# ---------------------------------------------------------------------------
# These are the orchestrator's entry points. They first try the fast
# sync parser; only if that raises do they call the repairer and re-try
# once on the repaired text. The sync parsers above are unchanged so
# tests, CLI tools, and any non-async caller keep working.


async def _try_repair(
    response: str,
    repairer: "FormatRepairerPort",
    schema_hint: str,
    original_error: Exception,
) -> str | None:
    """Invoke the LLM repairer with full failure tolerance.

    Returns the repaired text on success, ``None`` if the repairer should
    be treated as unavailable (call raised, returned no-op, or returned
    empty). Callers must surface ``original_error`` when this returns
    ``None`` — the LLM repairer is auxiliary; its failure must never
    mask the genuine mechanical-parse problem.
    """
    try:
        repaired = await repairer.repair(response, schema_hint)
    except Exception as repair_exc:  # noqa: BLE001 — auxiliary, must not propagate
        logger.warning(
            "Format repairer raised (%s); falling back to mechanical-parser "
            "error: %s",
            repair_exc,
            original_error,
        )
        return None
    if not repaired or repaired == response:
        # Adapter's documented no-op: LLM call failed and repaired == raw.
        # Treat as "repairer unavailable".
        return None
    return repaired


async def parse_assessment_response_async(
    response: str,
    repairer: "FormatRepairerPort | None" = None,
) -> AssessmentResult:
    """Parse an assessment response, with LLM-repair fallback on failure.

    Behaviour matrix:
    - repairer is None         → identical to :func:`parse_assessment_response`.
    - mechanical parse OK      → repairer not invoked.
    - mechanical fails, repair
      OK and reparse OK        → repaired result returned.
    - mechanical fails, repair
      no-op / errors / repaired
      reparse fails            → ORIGINAL mechanical error raised.
    """
    try:
        return parse_assessment_response(response)
    except (json.JSONDecodeError, ValueError, KeyError) as first_error:
        if repairer is None:
            raise
        logger.warning(
            "Assessment parse failed (%s); invoking format repairer",
            first_error,
        )
        repaired = await _try_repair(
            response, repairer, ASSESSMENT_SCHEMA_HINT, first_error,
        )
        if repaired is None:
            raise
        try:
            return parse_assessment_response(repaired)
        except (json.JSONDecodeError, ValueError, KeyError) as second_error:
            logger.warning(
                "Assessment parse failed even after format repair (%s); "
                "surfacing original error",
                second_error,
            )
            raise first_error from second_error


async def parse_subtasks_from_llm_async(
    response: str,
    repairer: "FormatRepairerPort | None" = None,
) -> list[Subtask]:
    """Parse a decomposition response, with LLM-repair fallback on failure.

    Same behaviour matrix as :func:`parse_assessment_response_async`.
    ``InfeasibleError`` is never repaired — it represents a semantic
    decision by the source model, not a format problem.
    """
    try:
        return parse_subtasks_from_llm(response)
    except InfeasibleError:
        raise
    except (json.JSONDecodeError, ValueError, KeyError) as first_error:
        if repairer is None:
            raise
        logger.warning(
            "Subtasks parse failed (%s); invoking format repairer",
            first_error,
        )
        repaired = await _try_repair(
            response, repairer, SUBTASKS_SCHEMA_HINT, first_error,
        )
        if repaired is None:
            raise
        try:
            return parse_subtasks_from_llm(repaired)
        except InfeasibleError:
            raise
        except (json.JSONDecodeError, ValueError, KeyError) as second_error:
            logger.warning(
                "Subtasks parse failed even after format repair (%s); "
                "surfacing original error",
                second_error,
            )
            raise first_error from second_error
