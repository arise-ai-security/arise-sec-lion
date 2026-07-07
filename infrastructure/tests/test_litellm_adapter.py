"""Integration tests for LiteLLMAdapter.

This module tests the LiteLLMAdapter infrastructure component, which wraps
the LiteLLM library to provide unified access to multiple LLM providers.

Tests use mocking to avoid real API calls and costs during testing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.application.services.prompt.cache_breakpoint import CACHE_BREAKPOINT_MARKER
from core.domain.exceptions import LLMError
from infrastructure.adapters.anthropic_cache import apply_anthropic_cache_to_messages
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter


@pytest.fixture
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace `asyncio.sleep` in robust_call with an AsyncMock.

    Retry tests would otherwise wait the full backoff window
    (`base * 2**i` with jitter -- tens of seconds per test) before
    asserting outcomes. Tests that need to inspect recorded delays
    use the returned mock; others just rely on the patch to keep
    wall time low.
    """
    sleep_mock = AsyncMock()
    import infrastructure.io.robust_call as _rc
    monkeypatch.setattr(_rc.asyncio, "sleep", sleep_mock)
    return sleep_mock


@pytest.mark.asyncio
async def test_litellm_adapter_successful_query() -> None:
    """Test that LiteLLMAdapter successfully queries an LLM."""

    # Given: Create adapter with default config
    adapter = LiteLLMAdapter(default_config={"model": "gpt-4", "temperature": 0.7})

    # And: Mock litellm.acompletion to return a fake response
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "This is a test response from the LLM"

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.return_value = mock_response

        # When: Query the adapter
        result = await adapter.query(prompt="What is 2+2?", config_dict={"max_tokens": 100})

        # Then: Verify the response is returned
        assert result == "This is a test response from the LLM"

        # And: Verify litellm.acompletion was called with correct parameters
        mock_acompletion.assert_called_once()
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4"
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 100
        assert call_kwargs["messages"] == [{"role": "user", "content": "What is 2+2?"}]


@pytest.mark.asyncio
async def test_litellm_adapter_config_override() -> None:
    """Test that query config overrides default config."""

    # Given: Create adapter with default temperature
    adapter = LiteLLMAdapter(default_config={"model": "gpt-4", "temperature": 0.7})

    # And: Mock litellm.acompletion
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Response"

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.return_value = mock_response

        # When: Query with different temperature
        await adapter.query(prompt="Test", config_dict={"temperature": 0.2, "max_tokens": 500})

        # Then: Verify override took effect
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["temperature"] == 0.2  # Overridden
        assert call_kwargs["max_tokens"] == 500  # New parameter
        assert call_kwargs["model"] == "gpt-4"  # From defaults


@pytest.mark.asyncio
async def test_litellm_adapter_empty_response() -> None:
    """Test that LiteLLMAdapter raises LLMError when response is empty."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to return None content
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = None

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.return_value = mock_response

        # When/Then: Query raises LLMError
        with pytest.raises(LLMError, match=r"(?i)empty response"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_authentication_error() -> None:
    """Test that LiteLLMAdapter wraps authentication errors in LLMError."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise AuthenticationError
    import litellm.exceptions

    auth_error = litellm.exceptions.AuthenticationError(
        message="Invalid API key",
        llm_provider="openai",
        model="gpt-4",
    )

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = auth_error

        # When/Then: Query raises LLMError with authentication message
        with pytest.raises(LLMError, match=r"(?i)authentication failed.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_rate_limit_error(_no_real_sleep: AsyncMock) -> None:
    """Test that LiteLLMAdapter wraps rate limit errors in LLMError."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise RateLimitError
    import litellm.exceptions

    rate_limit_error = litellm.exceptions.RateLimitError(
        message="Rate limit exceeded",
        llm_provider="openai",
        model="gpt-4",
    )

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = rate_limit_error

        # When/Then: Query raises LLMError with rate limit message
        with pytest.raises(LLMError, match=r"(?i)rate limit exceeded.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_api_error() -> None:
    """Test that LiteLLMAdapter wraps API errors in LLMError."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise APIError
    import litellm.exceptions

    api_error = litellm.exceptions.APIError(
        status_code=500,
        message="Internal server error",
        llm_provider="openai",
        model="gpt-4",
    )

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = api_error

        # When/Then: Query raises LLMError with API error message
        with pytest.raises(LLMError, match=r"(?i)api error.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_timeout_error() -> None:
    """Test that LiteLLMAdapter wraps timeout errors in LLMError."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise Timeout
    import litellm.exceptions

    timeout_error = litellm.exceptions.Timeout(
        message="Request timed out",
        model="gpt-4",
        llm_provider="openai",
    )

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = timeout_error

        # When/Then: Query raises LLMError with timeout message
        with pytest.raises(LLMError, match=r"(?i)timed out.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_service_unavailable_error(
    _no_real_sleep: AsyncMock,
) -> None:
    """Test that LiteLLMAdapter wraps service unavailable errors in LLMError.

    Note: `ServiceUnavailableError` is retryable; after the retries are
    exhausted the wrapper raises the unified rate-limit-style message.
    """

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise ServiceUnavailableError
    import litellm.exceptions

    service_error = litellm.exceptions.ServiceUnavailableError(
        message="Service is down",
        model="gpt-4",
        llm_provider="openai",
    )

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = service_error

        # When/Then: Query raises LLMError after retries are exhausted.
        # ServiceUnavailable is treated like RateLimit for retry; the
        # wrapper raises the unified "rate limit exceeded" envelope.
        with pytest.raises(LLMError, match=r"(?i)rate limit exceeded.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_unexpected_error() -> None:
    """Test that LiteLLMAdapter wraps unexpected errors in LLMError."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise unexpected exception
    unexpected_error = ValueError("Something went wrong")

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = unexpected_error

        # When/Then: Query raises LLMError with unexpected error message
        with pytest.raises(LLMError, match=r"(?i)unexpected.*error.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_preserves_original_error(
    _no_real_sleep: AsyncMock,
) -> None:
    """Test that LLMError preserves the original exception."""

    # Given: Create adapter
    adapter = LiteLLMAdapter()

    # And: Mock litellm.acompletion to raise an error
    import litellm.exceptions

    original_error = litellm.exceptions.RateLimitError(
        message="Rate limit",
        llm_provider="openai",
        model="gpt-4",
    )

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_acompletion:
        mock_acompletion.side_effect = original_error

        # When: Query raises LLMError
        with pytest.raises(LLMError) as exc_info:
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})

        # Then: Verify original error is preserved
        raised_error = exc_info.value
        assert raised_error.original_error is original_error
        assert "Rate limit" in str(raised_error)


@pytest.mark.asyncio
async def test_rate_limit_retry_uses_jitter(
    _no_real_sleep: AsyncMock,
) -> None:
    """RateLimitError retries use jittered exponential backoff via robust_call.

    Mocks `litellm.acompletion` to raise `RateLimitError` three times
    then succeed, captures every `asyncio.sleep` argument, and asserts:

    1. At least two distinct delays were observed (jitter active --
       deterministic backoff would emit identical multipliers).
    2. Each recorded delay lies inside the expected jitter window
       `[base * 2**i * 0.5, base * 2**i * 1.5]` for attempt index `i`.
    """

    # Given: an adapter
    adapter = LiteLLMAdapter()

    # And: litellm.acompletion fails with RateLimitError three times, then
    # returns a usable mock response on the fourth attempt
    import litellm.exceptions

    rate_limit_error = litellm.exceptions.RateLimitError(
        message="Rate limit exceeded",
        llm_provider="openai",
        model="gpt-4",
    )
    success_response = MagicMock()
    success_response.choices = [MagicMock()]
    success_response.choices[0].message.content = "ok"

    expected_retries = LiteLLMAdapter._RATE_LIMIT_RETRIES
    expected_attempts = expected_retries + 1
    side_effects = [rate_limit_error] * expected_retries + [success_response]

    with patch(
        "infrastructure.adapters.litellm_adapter.litellm.acompletion"
    ) as mock_acompletion:
        mock_acompletion.side_effect = side_effects

        result = await adapter.query(
            prompt="Test", config_dict={"model": "gpt-4"}
        )

    assert result == "ok"
    assert mock_acompletion.call_count == expected_attempts

    sleep_calls = [c.args[0] for c in _no_real_sleep.call_args_list]
    assert len(sleep_calls) == expected_retries, sleep_calls

    base = LiteLLMAdapter._RATE_LIMIT_BASE_DELAY
    backoff = 2.0
    for i, delay in enumerate(sleep_calls):
        planned = base * (backoff**i)
        lower = planned * 0.5
        upper = planned * 1.5
        assert lower <= delay <= upper, (
            f"attempt {i}: delay {delay} not in [{lower}, {upper}]"
        )


def _usage(
    *,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    cached_tokens: int | None = None,
) -> MagicMock:
    """Build a usage stub exposing only the attributes a real response carries.

    Using ``spec`` ensures absent cache fields are genuinely missing (not
    auto-vivified MagicMocks), exercising the adapter's defensive getattr.
    """
    spec = ["prompt_tokens", "completion_tokens", "total_tokens"]
    if cache_read_input_tokens is not None:
        spec.append("cache_read_input_tokens")
    if cache_creation_input_tokens is not None:
        spec.append("cache_creation_input_tokens")
    if cached_tokens is not None:
        spec.append("prompt_tokens_details")
    usage = MagicMock(spec=spec)
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15
    if cache_read_input_tokens is not None:
        usage.cache_read_input_tokens = cache_read_input_tokens
    if cache_creation_input_tokens is not None:
        usage.cache_creation_input_tokens = cache_creation_input_tokens
    if cached_tokens is not None:
        details = MagicMock(spec=["cached_tokens"])
        details.cached_tokens = cached_tokens
        usage.prompt_tokens_details = details
    return usage


@pytest.mark.asyncio
async def test_query_with_usage_maps_anthropic_cache_tokens() -> None:
    # Given: a response whose usage carries normalized Anthropic cache stats
    adapter = LiteLLMAdapter(default_config={"model": "claude-3-5-sonnet"})
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    response.usage = _usage(cache_read_input_tokens=40, cache_creation_input_tokens=12)

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_call:
        mock_call.return_value = response
        # When: querying with usage extraction
        result = await adapter.query_with_usage(prompt="hi", config_dict={})

    # Then: cache read/write tokens map onto LLMUsage
    assert result.usage.cache_read_tokens == 40
    assert result.usage.cache_write_tokens == 12


@pytest.mark.asyncio
async def test_query_with_usage_falls_back_to_cached_tokens_for_read() -> None:
    # Given: a response that only exposes the OpenAI-style cached_tokens detail
    adapter = LiteLLMAdapter(default_config={"model": "gpt-4"})
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    response.usage = _usage(cached_tokens=25)

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_call:
        mock_call.return_value = response
        # When: querying with usage extraction
        result = await adapter.query_with_usage(prompt="hi", config_dict={})

    # Then: cached_tokens is used as the cache read count; write stays zero
    assert result.usage.cache_read_tokens == 25
    assert result.usage.cache_write_tokens == 0


@pytest.mark.asyncio
async def test_query_applies_cache_control_for_claude_on_no_tools_path() -> None:
    # Given: a Claude model on the plain (no-tools) query path
    adapter = LiteLLMAdapter(default_config={"model": "claude-3-5-sonnet"})
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_call:
        mock_call.return_value = response
        # When: issuing a plain query
        await adapter.query(prompt="hi", config_dict={})

    # Then: the first user message carries the ephemeral cache_control marker
    first_user = mock_call.call_args.kwargs["messages"][0]
    assert first_user["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_query_leaves_messages_plain_for_non_claude() -> None:
    # Given: a non-Claude model that must not receive cache_control
    adapter = LiteLLMAdapter(default_config={"model": "gpt-4"})
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"

    with patch("infrastructure.adapters.litellm_adapter.litellm.acompletion") as mock_call:
        mock_call.return_value = response
        # When: issuing a plain query
        await adapter.query(prompt="hi", config_dict={})

    # Then: the message content is left as a plain string (no cache_control)
    first_user = mock_call.call_args.kwargs["messages"][0]
    assert first_user["content"] == "hi"


def test_anthropic_marker_splits_into_static_and_variable_blocks() -> None:
    # Given: a cache-optimized first user message for an Anthropic model
    content = f"static prefix\n\n{CACHE_BREAKPOINT_MARKER}\n\nvariable tail"
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    # When: cache control is applied for an Anthropic model
    result = apply_anthropic_cache_to_messages(messages, "claude-3-opus")
    # Then: the message becomes two text blocks, only the static one cached.
    # The "\n\n" seam trimmed by the split is restored on the variable block
    # so the blocks concatenate to the original prompt bytes (Anthropic joins
    # text blocks with no separator).
    blocks = result[0]["content"]
    assert blocks == [
        {"type": "text", "text": "static prefix", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\nvariable tail"},
    ]
    assert blocks[0]["text"] + blocks[1]["text"] == "static prefix\n\nvariable tail"
    # And: no block text leaks the marker
    assert all(CACHE_BREAKPOINT_MARKER not in b["text"] for b in blocks)


def test_non_anthropic_marker_strips_to_plain_string() -> None:
    # Given: a cache-optimized first user message for a non-Anthropic model
    content = f"static prefix\n\n{CACHE_BREAKPOINT_MARKER}\n\nvariable tail"
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    # When: cache control is applied for a non-Anthropic model
    result = apply_anthropic_cache_to_messages(messages, "gpt-4o")
    # Then: the content stays a plain string with the marker removed
    assert result[0]["content"] == "static prefix\n\nvariable tail"
    assert CACHE_BREAKPOINT_MARKER not in result[0]["content"]


def test_anthropic_no_marker_keeps_single_cached_block() -> None:
    # Given: a first user message without a marker for an Anthropic model
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]
    # When: cache control is applied for an Anthropic model
    result = apply_anthropic_cache_to_messages(messages, "claude-3-opus")
    # Then: prior behavior is preserved -- a single cached block
    assert result[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]


def test_non_anthropic_no_marker_is_unchanged() -> None:
    # Given: a first user message without a marker for a non-Anthropic model
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]
    # When: cache control is applied for a non-Anthropic model
    result = apply_anthropic_cache_to_messages(messages, "gpt-4o")
    # Then: the message is returned unchanged
    assert result[0]["content"] == "hello"
