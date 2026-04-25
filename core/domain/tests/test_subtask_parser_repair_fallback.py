"""Tests for the LLM-repair fallback path in async parser variants.

Scope:
- The async variants behave identically to the sync ones when the
  repairer is None or the input parses cleanly (no extra LLM calls).
- On parse failure, the async variants invoke ``repairer.repair`` with
  the correct schema hint and re-parse the repaired output.
- If the repairer also fails (or returns the same text), the original
  parse error is surfaced (no infinite loop, no masking).
- Schema hints are derived from current Pydantic models — adding a
  required field to ``Subtask`` propagates into the hint.
"""

from typing import Any

import pytest

from core.domain.services import (
    ASSESSMENT_SCHEMA_HINT,
    SUBTASKS_SCHEMA_HINT,
    parse_assessment_response_async,
    parse_subtasks_from_llm_async,
)
from core.domain.values.subtask import Subtask


_VALID_SUBTASK_JSON = (
    '[{"description": "Sub 1", "config": {"strategy": "heuristic", '
    '"base": {"model": "gpt-4o-mini", "temperature": 0.5, '
    '"max_tokens": 4000}, "tool": "claude_code"}}]'
)
_VALID_DECOMPOSE_ASSESSMENT = (
    '{"action": "decompose", "reasoning": "split", '
    f'"subtasks": {_VALID_SUBTASK_JSON}}}'
)


class _FakeRepairer:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    async def repair(self, raw: str, schema_hint: str) -> str:
        self.calls.append((raw, schema_hint))
        return self._response


# ---------------------------------------------------------------------------
# Happy path: repairer is not invoked when input is already parseable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_async_does_not_call_repairer_for_valid_input() -> None:
    repairer = _FakeRepairer(response="should-not-be-used")
    result = await parse_assessment_response_async(
        '{"action": "execute", "reasoning": "ok"}',
        repairer=repairer,
    )
    assert result.action == "execute"
    assert repairer.calls == []


@pytest.mark.asyncio
async def test_subtasks_async_does_not_call_repairer_for_valid_input() -> None:
    repairer = _FakeRepairer(response="should-not-be-used")
    out = await parse_subtasks_from_llm_async(_VALID_SUBTASK_JSON, repairer=repairer)
    assert len(out) == 1
    assert out[0].description == "Sub 1"
    assert repairer.calls == []


# ---------------------------------------------------------------------------
# Failure path: repairer is invoked with the right schema hint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_async_invokes_repairer_with_assessment_schema() -> None:
    # Given: malformed input that the standard pipeline cannot parse,
    # and a repairer that returns a clean assessment response.
    malformed = "this is not JSON at all"
    repairer = _FakeRepairer(response='{"action": "execute", "reasoning": "ok"}')

    # When: the async parser is invoked
    result = await parse_assessment_response_async(malformed, repairer=repairer)

    # Then: the result reflects the repaired text, and the repairer was
    # given the malformed input plus the assessment schema hint.
    assert result.action == "execute"
    assert len(repairer.calls) == 1
    raw, hint = repairer.calls[0]
    assert raw == malformed
    assert hint is ASSESSMENT_SCHEMA_HINT


@pytest.mark.asyncio
async def test_subtasks_async_invokes_repairer_with_subtasks_schema() -> None:
    malformed = "still not JSON"
    repairer = _FakeRepairer(response=_VALID_SUBTASK_JSON)
    out = await parse_subtasks_from_llm_async(malformed, repairer=repairer)
    assert len(out) == 1
    assert len(repairer.calls) == 1
    raw, hint = repairer.calls[0]
    assert raw == malformed
    assert hint is SUBTASKS_SCHEMA_HINT


# ---------------------------------------------------------------------------
# Repair-of-repair fails → surface the ORIGINAL error, not the second one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_async_surfaces_original_error_when_repair_also_invalid() -> None:
    repairer = _FakeRepairer(response="still garbage")
    with pytest.raises((ValueError, Exception)):
        await parse_assessment_response_async("garbage }{ ", repairer=repairer)
    # Repairer was attempted exactly once
    assert len(repairer.calls) == 1


@pytest.mark.asyncio
async def test_assessment_async_surfaces_original_error_when_repair_noops() -> None:
    """If the repairer returns the same text (e.g. its LLM call failed),
    the caller's original parse error should be raised — not silently swallowed."""
    raw = "garbage }{ "
    repairer = _FakeRepairer(response=raw)  # noop = same text back
    with pytest.raises((ValueError, Exception)):
        await parse_assessment_response_async(raw, repairer=repairer)


class _RaisingRepairer:
    """A repairer whose .repair() itself raises an unexpected exception."""

    async def repair(self, raw: str, schema_hint: str) -> str:
        raise RuntimeError("ollama unreachable / auth failure / bug")


@pytest.mark.asyncio
async def test_assessment_async_falls_back_when_repairer_itself_raises() -> None:
    """LLM repairer is auxiliary — its own exceptions must surface the
    original mechanical-parse error, not crash the caller with the LLM
    exception."""
    repairer = _RaisingRepairer()
    with pytest.raises((ValueError, Exception)) as exc_info:
        await parse_assessment_response_async("garbage }{ ", repairer=repairer)
    # The exception we see must be the parse error, not the runtime error.
    assert "ollama" not in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_subtasks_async_falls_back_when_repairer_itself_raises() -> None:
    repairer = _RaisingRepairer()
    with pytest.raises((ValueError, Exception)) as exc_info:
        await parse_subtasks_from_llm_async("garbage }{ ", repairer=repairer)
    assert "ollama" not in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_assessment_async_falls_back_when_repairer_returns_empty() -> None:
    """An empty string from the repairer (timeout / blank response) is
    indistinguishable from a no-op and must surface the original error."""
    repairer = _FakeRepairer(response="")
    with pytest.raises((ValueError, Exception)):
        await parse_assessment_response_async("garbage }{ ", repairer=repairer)


# ---------------------------------------------------------------------------
# Schema hints stay in sync with Pydantic models (the dynamic-adjustment
# property the design promises).
# ---------------------------------------------------------------------------


def test_subtasks_schema_hint_contains_every_subtask_field() -> None:
    fields = Subtask.model_json_schema().get("properties", {})
    # The hint embeds the full Pydantic schema, so every field name from
    # the model must appear in the hint string.
    for field_name in fields:
        assert field_name in SUBTASKS_SCHEMA_HINT, (
            f"Field {field_name!r} is in Subtask but missing from "
            f"SUBTASKS_SCHEMA_HINT — hint generation drift."
        )


def test_assessment_schema_hint_contains_subtask_field_names() -> None:
    fields = Subtask.model_json_schema().get("properties", {})
    for field_name in fields:
        assert field_name in ASSESSMENT_SCHEMA_HINT, (
            f"Field {field_name!r} is in Subtask but missing from "
            f"ASSESSMENT_SCHEMA_HINT — hint generation drift."
        )


# ---------------------------------------------------------------------------
# Backward compatibility: passing repairer=None falls through to the sync
# behaviour exactly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_async_without_repairer_matches_sync_behaviour() -> None:
    with pytest.raises((ValueError, Exception)):
        await parse_assessment_response_async("garbage", repairer=None)


@pytest.mark.asyncio
async def test_assessment_async_without_repairer_passes_through_valid_input() -> None:
    result = await parse_assessment_response_async(
        '{"action": "execute"}', repairer=None,
    )
    assert result.action == "execute"
