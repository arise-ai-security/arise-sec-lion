"""Integration tests for LiteLLMAdapter.

This module tests the LiteLLMAdapter infrastructure component, which wraps
the LiteLLM library to provide unified access to multiple LLM providers.

Tests use mocking to avoid real API calls and costs during testing.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.domain.exceptions import LLMError
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter


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
async def test_litellm_adapter_rate_limit_error() -> None:
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
async def test_litellm_adapter_service_unavailable_error() -> None:
    """Test that LiteLLMAdapter wraps service unavailable errors in LLMError."""

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

        # When/Then: Query raises LLMError with service unavailable message
        with pytest.raises(LLMError, match=r"(?i)service unavailable.*gpt-4"):
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
        with pytest.raises(LLMError, match=r"(?i)unexpected error.*gpt-4"):
            await adapter.query(prompt="Test", config_dict={"model": "gpt-4"})


@pytest.mark.asyncio
async def test_litellm_adapter_preserves_original_error() -> None:
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
