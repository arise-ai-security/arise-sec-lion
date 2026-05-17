"""Tests for ``OpenRouterAdapter``.

Mocks ``openai.AsyncOpenAI`` so no real HTTP/credit is consumed. Mirrors the
shape of ``test_litellm_adapter.py``: each test sets up a fake response,
patches the SDK call, exercises the adapter, asserts on outputs and the
arguments forwarded to the SDK.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import (
    APIStatusError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

from core.domain.exceptions import LLMError
from infrastructure.adapters.openrouter_adapter import OpenRouterAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace ``asyncio.sleep`` in ``robust_call`` so retries do not block."""
    sleep_mock = AsyncMock()
    import infrastructure.io.robust_call as _rc

    monkeypatch.setattr(_rc.asyncio, "sleep", sleep_mock)
    return sleep_mock


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``OPENROUTER_API_KEY`` is set so adapter construction succeeds."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake")


def _make_response(
    content: str | None = "ok",
    *,
    tool_calls: list[Any] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost: float | None = 0.000123,
) -> MagicMock:
    """Build a MagicMock matching the OpenAI/OpenRouter response shape."""
    response = MagicMock()
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = message
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens
    usage.cost = cost
    response.usage = usage
    return response


def _api_error(status: int, message: str) -> APIStatusError:
    """Synthesize an APIStatusError without needing a real httpx Response."""
    request = MagicMock()
    response = MagicMock()
    response.status_code = status
    return APIStatusError(message=message, response=response, body=None)


def _internal_server_error(message: str = "upstream down") -> InternalServerError:
    """Synthesize an InternalServerError (5xx) — retryable."""
    request = MagicMock()
    response = MagicMock()
    response.status_code = 503
    return InternalServerError(message=message, response=response, body=None)


def _rate_limit_error() -> RateLimitError:
    request = MagicMock()
    response = MagicMock()
    response.status_code = 429
    return RateLimitError(message="rate limited", response=response, body=None)


def _auth_error() -> AuthenticationError:
    request = MagicMock()
    response = MagicMock()
    response.status_code = 401
    return AuthenticationError(message="bad key", response=response, body=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_openrouter_adapter_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing without an API key (env + arg both absent) raises immediately."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterAdapter()


@pytest.mark.asyncio
async def test_query_returns_content_and_forwards_model() -> None:
    """``query`` returns the message content and forwards model + temperature."""
    adapter = OpenRouterAdapter(
        default_config={"model": "openai/gpt-4o-mini", "temperature": 0.5}
    )
    response = _make_response(content="hello")

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ) as mock_create:
        result = await adapter.query(prompt="hi", config_dict={"max_tokens": 100})

    assert result == "hello"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["temperature"] == 0.5
    assert kwargs["max_tokens"] == 100
    # Per-call cost included in OpenRouter response is enabled via extra_body.
    assert kwargs["extra_body"] == {"usage": {"include": True}}


@pytest.mark.asyncio
async def test_query_with_usage_reports_response_cost() -> None:
    """``query_with_usage`` prefers the cost OpenRouter reports in ``usage.cost``."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})
    response = _make_response(
        content="ok", prompt_tokens=12, completion_tokens=4, cost=0.00077
    )

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ):
        result = await adapter.query_with_usage(prompt="x", config_dict={})

    assert result.content == "ok"
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 4
    assert result.usage.total_tokens == 16
    assert result.model == "openai/gpt-4o-mini"
    assert result.cost_usd == 0.00077


@pytest.mark.asyncio
async def test_query_with_usage_falls_back_to_calculator_when_cost_absent() -> None:
    """If OpenRouter omits ``usage.cost``, the cost calculator is consulted."""
    calculator = MagicMock()
    calculator.calculate_llm_cost = MagicMock(return_value=0.5)
    adapter = OpenRouterAdapter(
        default_config={"model": "anthropic/claude-haiku-4.5"},
        cost_calculator=calculator,
    )
    response = _make_response(
        content="ok", prompt_tokens=10, completion_tokens=20, cost=None
    )

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ):
        result = await adapter.query_with_usage(prompt="x", config_dict={})

    assert result.cost_usd == 0.5
    calculator.calculate_llm_cost.assert_called_once_with(
        "anthropic/claude-haiku-4.5", 10, 20
    )


@pytest.mark.asyncio
async def test_query_with_usage_cost_zero_when_no_source() -> None:
    """No response cost AND no calculator → cost defaults to 0.0 (never raises)."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})
    response = _make_response(content="ok", cost=None)

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ):
        result = await adapter.query_with_usage(prompt="x", config_dict={})

    assert result.cost_usd == 0.0


@pytest.mark.asyncio
async def test_query_empty_response_raises() -> None:
    """A response with ``content=None`` becomes a clean ``LLMError``."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})
    response = _make_response(content=None)

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ), pytest.raises(LLMError, match="empty response"):
        await adapter.query(prompt="x", config_dict={})


@pytest.mark.asyncio
async def test_o_series_model_omits_temperature() -> None:
    """OpenAI o-series and gpt-5 models must NOT receive a ``temperature`` arg."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/o3-mini"})
    response = _make_response()

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ) as mock_create:
        await adapter.query(prompt="x", config_dict={})

    assert "temperature" not in mock_create.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_real_sleep")
async def test_rate_limit_error_propagates_as_llmerror() -> None:
    """After exhausting retries on 429, the adapter raises ``LLMError``."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})

    with patch.object(
        adapter._client.chat.completions,
        "create",
        AsyncMock(side_effect=_rate_limit_error()),
    ), pytest.raises(LLMError, match="rate-limited"):
        await adapter.query(prompt="x", config_dict={})


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_real_sleep")
async def test_rate_limit_retries_then_succeeds() -> None:
    """A 429 on the first attempt is retried; a success on the retry returns content."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})
    response = _make_response(content="ok")

    side_effects: list[Any] = [
        _rate_limit_error(),
        response,
    ]

    with patch.object(
        adapter._client.chat.completions,
        "create",
        AsyncMock(side_effect=side_effects),
    ) as mock_create:
        result = await adapter.query(prompt="x", config_dict={})

    assert result == "ok"
    assert mock_create.call_count == 2


@pytest.mark.asyncio
async def test_authentication_error_does_not_retry() -> None:
    """Auth errors are not retryable — they propagate on the first attempt."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})

    with patch.object(
        adapter._client.chat.completions,
        "create",
        AsyncMock(side_effect=_auth_error()),
    ) as mock_create, pytest.raises(LLMError, match="authentication failed"):
        await adapter.query(prompt="x", config_dict={})

    assert mock_create.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_real_sleep")
async def test_api_status_5xx_retried_then_propagates() -> None:
    """5xx errors are retryable (transient); final failure becomes ``LLMError``."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})

    side_effects = [
        _internal_server_error("upstream down")
        for _ in range(OpenRouterAdapter._RATE_LIMIT_RETRIES + 1)
    ]
    with patch.object(
        adapter._client.chat.completions,
        "create",
        AsyncMock(side_effect=side_effects),
    ) as mock_create, pytest.raises(LLMError, match="API error"):
        await adapter.query(prompt="x", config_dict={})

    assert mock_create.call_count == OpenRouterAdapter._RATE_LIMIT_RETRIES + 1


@pytest.mark.asyncio
async def test_api_status_4xx_does_not_retry() -> None:
    """A non-retryable APIStatusError (e.g. 400) propagates on the first attempt."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})

    with patch.object(
        adapter._client.chat.completions,
        "create",
        AsyncMock(side_effect=_api_error(400, "bad request")),
    ) as mock_create, pytest.raises(LLMError, match="API error"):
        await adapter.query(prompt="x", config_dict={})

    assert mock_create.call_count == 1


@pytest.mark.asyncio
async def test_query_with_tools_returns_tool_calls() -> None:
    """Structured ``tool_calls`` from the API are returned as ``ToolCall`` objects."""
    adapter = OpenRouterAdapter(default_config={"model": "openai/gpt-4o-mini"})
    tool_call = MagicMock()
    tool_call.id = "call_123"
    tool_call.function = MagicMock()
    tool_call.function.name = "search_codebase"
    tool_call.function.arguments = '{"query": "skip_white"}'
    response = _make_response(content=None, tool_calls=[tool_call])

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_codebase",
                "parameters": {"properties": {"query": {"type": "string"}}},
            },
        }
    ]
    messages = [{"role": "user", "content": "find the bug"}]

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ):
        result = await adapter.query_with_tools(
            messages=messages, config_dict={}, tools=tools
        )

    assert result.content is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_123"
    assert result.tool_calls[0].name == "search_codebase"
    assert result.tool_calls[0].arguments == {"query": "skip_white"}


@pytest.mark.asyncio
async def test_query_with_tools_strips_tool_messages_when_no_tools() -> None:
    """Empty ``tools`` triggers the Anthropic-safe message cleanup."""
    adapter = OpenRouterAdapter(default_config={"model": "anthropic/claude-haiku-4.5"})
    response = _make_response(content="final answer")

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "x", "type": "function", "function": {"name": "f"}}],
        },
        {"role": "tool", "content": "tool result", "tool_call_id": "x"},
    ]

    with patch.object(
        adapter._client.chat.completions, "create", AsyncMock(return_value=response)
    ) as mock_create:
        result = await adapter.query_with_tools(
            messages=messages, config_dict={}, tools=[]
        )

    assert result.content == "final answer"
    forwarded = mock_create.call_args.kwargs["messages"]
    assert all(m.get("role") != "tool" for m in forwarded)
    assert all("tool_calls" not in m for m in forwarded)
