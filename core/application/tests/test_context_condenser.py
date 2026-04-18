"""Tests for the recon context condenser.

Verifies that:
- The condenser's LLM summarization call uses ``query_with_usage`` and emits a
  ``TokensConsumed`` event with ``operation='context_condense'`` when an agent
  is supplied.
- When the LLM call raises, the deterministic fallback still produces a string
  and NO ``TokensConsumed`` event is emitted.
- When no agent is supplied (backwards-compatible path), the LLM call still
  succeeds without emitting an event.
"""

from typing import Any

import pytest

from core.application.services.orchestration.context_condenser import ContextCondenser
from core.domain.values.llm_response import LLMResponse, LLMUsage


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLLMPort:
    """LLM port that returns a scripted ``LLMResponse`` from ``query_with_usage``."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.usage_calls: list[tuple[str, dict[str, Any]]] = []
        self.legacy_calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        self.legacy_calls.append((prompt, config_dict))
        return self._response.content

    async def query_with_usage(
        self, prompt: str, config_dict: dict[str, Any]
    ) -> LLMResponse:
        self.usage_calls.append((prompt, config_dict))
        return self._response

    async def query_with_tools(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class FailingLLMPort:
    """LLM port whose summarization call always raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.usage_calls = 0

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        raise self._exc

    async def query_with_usage(
        self, prompt: str, config_dict: dict[str, Any]
    ) -> LLMResponse:
        self.usage_calls += 1
        raise self._exc

    async def query_with_tools(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class FakeAgent:
    """Minimal agent capturing ``emit_tokens_consumed`` calls."""

    def __init__(self) -> None:
        self.tokens_consumed_calls: list[dict[str, Any]] = []

    def emit_tokens_consumed(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float,
        operation: str,
    ) -> None:
        self.tokens_consumed_calls.append(
            {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
                "operation": operation,
            }
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_messages() -> list[dict[str, Any]]:
    """Two-iteration tool exchange + a latest assistant+tool block.

    The condenser's ``maybe_condense`` keeps the prompt and the latest
    assistant+tool block verbatim and summarizes the rest.
    """
    return [
        {"role": "user", "content": "initial prompt"},
        # Old iteration (eligible for condensation)
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "print('hello')",
        },
        # Latest iteration (kept verbatim)
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "list_directory", "arguments": '{"path": "."}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": "file1.py\nfile2.py",
        },
    ]


def _response(content: str = "condensed summary") -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=LLMUsage(prompt_tokens=500, completion_tokens=50, total_tokens=550),
        model="gpt-4o-mini",
        cost_usd=0.0041,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condenser_emits_tokens_consumed_with_context_condense_operation() -> None:
    """Condenser LLM summarization emits TokensConsumed(operation='context_condense')."""

    # Given: a condenser with an LLM port that returns a populated LLMResponse
    llm_port = FakeLLMPort(response=_response())
    agent = FakeAgent()
    condenser = ContextCondenser(
        llm_port=llm_port,
        condense_after_iteration=1,
        condenser_model="gpt-4o-mini",
    )

    # When: maybe_condense summarizes the older exchanges
    result = await condenser.maybe_condense(
        _sample_messages(), current_iteration=2, agent=agent  # type: ignore[arg-type]
    )

    # Then: exactly one LLM summarization happened via query_with_usage
    assert len(llm_port.usage_calls) == 1
    assert llm_port.legacy_calls == []

    # And: the summary was inserted into the condensed history
    assert isinstance(result, list)
    assert any("[RECON SUMMARY" in (m.get("content") or "") for m in result)

    # And: exactly one TokensConsumed(operation='context_condense') was emitted
    assert len(agent.tokens_consumed_calls) == 1
    call = agent.tokens_consumed_calls[0]
    assert call["operation"] == "context_condense"
    assert call["model"] == "gpt-4o-mini"
    assert call["prompt_tokens"] == 500
    assert call["completion_tokens"] == 50
    assert call["total_tokens"] == 550
    assert call["cost_usd"] == 0.0041


@pytest.mark.asyncio
async def test_condenser_llm_failure_falls_back_without_emitting_tokens() -> None:
    """If the LLM call raises, condenser uses deterministic fallback and emits no TokensConsumed."""

    # Given: an LLM port whose query_with_usage always raises
    llm_port = FailingLLMPort(RuntimeError("timeout"))
    agent = FakeAgent()
    condenser = ContextCondenser(
        llm_port=llm_port,
        condense_after_iteration=1,
        condenser_model="gpt-4o-mini",
    )

    # When: maybe_condense runs on an eligible history
    result = await condenser.maybe_condense(
        _sample_messages(), current_iteration=2, agent=agent  # type: ignore[arg-type]
    )

    # Then: query_with_usage was attempted, fallback produced a summary
    assert llm_port.usage_calls == 1
    assert isinstance(result, list)
    assert any("[RECON SUMMARY" in (m.get("content") or "") for m in result)

    # And: no TokensConsumed emitted for the failed call
    assert agent.tokens_consumed_calls == []


@pytest.mark.asyncio
async def test_condenser_without_agent_skips_emission_but_still_summarizes() -> None:
    """Backwards-compatible path: no agent supplied → no emission, summary still produced."""

    # Given: a condenser but no agent threaded through
    llm_port = FakeLLMPort(response=_response(content="summary without agent"))
    condenser = ContextCondenser(
        llm_port=llm_port,
        condense_after_iteration=1,
        condenser_model="gpt-4o-mini",
    )

    # When: maybe_condense runs without an agent
    result = await condenser.maybe_condense(
        _sample_messages(), current_iteration=2
    )

    # Then: the LLM call still happened and a summary was produced
    assert len(llm_port.usage_calls) == 1
    assert any(
        "summary without agent" in (m.get("content") or "") for m in result
    )
