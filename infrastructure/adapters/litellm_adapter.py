"""LiteLLM adapter: unified interface to multiple LLM providers.

This adapter wraps LiteLLM to provide a unified interface for querying
various LLM providers (OpenAI, Anthropic, Google, etc.) with optional
cost tracking via the CostCalculatorPort.
"""

import json
import logging
from typing import Any

import litellm

from core.domain.exceptions import LLMError
from core.domain.values.llm_response import LLMResponse, LLMToolResponse, LLMUsage, ToolCall
from core.ports.runtime_ports import CostCalculatorPort, LLMPort


logger = logging.getLogger(__name__)


def _usage_int(source: Any, attr: str, default: int = 0) -> int:
    """Read an int attribute off a LiteLLM usage object defensively.

    LiteLLM passes provider-specific usage fields through as attributes on
    the ``Usage`` / ``*_tokens_details`` pydantic models. Older models don't
    carry the newer dimensions, so callers need to tolerate missing attrs,
    ``None``, and non-numeric inputs without crashing the LLM call.
    """
    if source is None:
        return default
    raw = getattr(source, attr, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


class LiteLLMAdapter(LLMPort):
    """Query LLMs via LiteLLM (OpenAI, Anthropic, etc.) with cost tracking."""

    def __init__(
        self,
        default_config: dict[str, Any] | None = None,
        cost_calculator: CostCalculatorPort | None = None,
    ) -> None:
        """Initialize LiteLLM adapter.

        Args:
            default_config: Default LLM configuration (model, temperature, etc.).
            cost_calculator: Optional cost calculator for pricing. If not provided,
                query_with_usage will use LiteLLM's built-in cost calculation.
        """
        self.default_config = default_config or {}
        self._cost_calculator = cost_calculator

    @staticmethod
    def _is_o_series(model: str) -> bool:
        """Return True for models that reject the ``temperature`` parameter.

        * OpenAI O-series (``o1``, ``o3``, ``o4``) have never supported it.
        * Anthropic ``claude-opus-4-7`` deprecated it (API returns a 400 with
          ``"temperature is deprecated for this model."``) — pending Anthropic
          publishing a broader list, extend conservatively to any ``opus-4-7``
          variant. Sonnet / Haiku 4.x still accept ``temperature`` as of
          2026-04.
        """
        base = model.split("/")[-1].lower()
        if base.startswith(("o1", "o3", "o4")):
            return True
        return "opus-4-7" in base or "opus-4.7" in base

    async def _call_litellm(self, model: str, **kwargs: Any) -> Any:
        """Call litellm.acompletion with unified exception handling.

        Args:
            model: Model identifier (e.g., 'gpt-4', 'claude-3-opus').
            **kwargs: Passed directly to litellm.acompletion (messages, tools, etc.).

        Returns:
            Raw LiteLLM response object.

        Raises:
            LLMError: On any API failure, with specific error messages.
        """
        try:
            return await litellm.acompletion(model=model, **kwargs)

        except litellm.exceptions.AuthenticationError as e:
            raise LLMError(
                f"LLM authentication failed for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.RateLimitError as e:
            raise LLMError(
                f"LLM rate limit exceeded for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.APIError as e:
            raise LLMError(
                f"LLM API error for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.Timeout as e:
            raise LLMError(
                f"LLM request timed out for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.ServiceUnavailableError as e:
            raise LLMError(
                f"LLM service unavailable for model '{model}': {e}",
                original_error=e,
            ) from e

        except Exception as e:
            raise LLMError(
                f"Unexpected LLM error for model '{model}': {e}",
                original_error=e,
            ) from e

    def _extract_usage(self, model: str, response: Any) -> tuple[LLMUsage, float]:
        """Extract token usage and cost from a raw LiteLLM response.

        Anthropic surfaces prompt-cache counts via ``prompt_tokens_details``
        (``cached_tokens`` for cache-read) and
        ``cache_creation_input_tokens``. OpenAI / reasoning models expose
        thinking tokens via ``completion_tokens_details.reasoning_tokens``.
        LiteLLM proxies the provider fields on the ``Usage`` object. Missing
        fields default to 0 so historical call sites keep working.

        Returns:
            Tuple of (LLMUsage, cost_usd).
        """
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else 0
        cache_read_tokens = _usage_int(usage, "cache_read_input_tokens", 0) if usage else 0
        if not cache_read_tokens and usage is not None:
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            cache_read_tokens = _usage_int(prompt_details, "cached_tokens", 0)
        cache_write_tokens = _usage_int(usage, "cache_creation_input_tokens", 0) if usage else 0
        reasoning_tokens = 0
        if usage is not None:
            completion_details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = _usage_int(completion_details, "reasoning_tokens", 0) or _usage_int(
                usage, "reasoning_tokens", 0
            )
        cost_usd = self._calculate_cost(
            model,
            prompt_tokens,
            completion_tokens,
            response,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

        return (
            LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
            cost_usd,
        )

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Query LLM and return response content.

        Args:
            prompt: The prompt to send to the LLM.
            config_dict: Configuration including model, temperature, max_tokens.

        Returns:
            Response content as string.

        Raises:
            LLMError: On API failure or empty response.
        """
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")

        call_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": merged_config.get("max_tokens", 1000),
            "top_p": merged_config.get("top_p"),
        }
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)

        response = await self._call_litellm(model=model, **call_kwargs)

        content = response.choices[0].message.content
        if content is None:
            raise LLMError("LLM returned empty response")
        return content

    async def query_with_usage(self, prompt: str, config_dict: dict[str, Any]) -> LLMResponse:
        """Query LLM and return response with usage and cost metadata.

        Args:
            prompt: The prompt to send to the LLM.
            config_dict: Configuration including model, temperature, max_tokens.

        Returns:
            LLMResponse with content, usage, model, and cost_usd.

        Raises:
            LLMError: On API failure or empty response.
        """
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")

        call_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": merged_config.get("max_tokens", 1000),
            "top_p": merged_config.get("top_p"),
        }
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)

        response = await self._call_litellm(model=model, **call_kwargs)

        content = response.choices[0].message.content
        if content is None:
            raise LLMError("LLM returned empty response")

        llm_usage, cost_usd = self._extract_usage(model, response)

        return LLMResponse(
            content=content,
            usage=llm_usage,
            model=model,
            cost_usd=cost_usd,
        )

    async def query_with_tools(
        self,
        messages: list[dict[str, Any]],
        config_dict: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        """Query LLM with tool definitions, returning content or tool calls.

        Uses litellm's native tool-calling support. The response either contains
        text content (final answer) or tool_calls (requesting tool execution).
        """
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")

        call_kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "max_tokens": merged_config.get("max_tokens", 4000),
            "top_p": merged_config.get("top_p"),
        }
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)

        response = await self._call_litellm(model=model, **call_kwargs)

        message = response.choices[0].message

        # Parse tool calls if present
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json.loads(args)
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        llm_usage, cost_usd = self._extract_usage(model, response)

        return LLMToolResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=llm_usage,
            model=model,
            cost_usd=cost_usd,
        )

    def _calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        response: Any,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Calculate cost using injected calculator or LiteLLM fallback.

        Args:
            model: Model name.
            prompt_tokens: Number of input tokens (includes cache tokens).
            completion_tokens: Number of output tokens.
            response: Raw LiteLLM response (for built-in cost calculation).
            cache_read_tokens: Subset of ``prompt_tokens`` billed at the
                cache-read rate. Defaults to 0 for providers without caching.
            cache_write_tokens: Subset of ``prompt_tokens`` billed at the
                cache-creation rate.

        Returns:
            Cost in USD.
        """
        # Use injected calculator if available. Bootstrap always injects
        # ``DefaultCostCalculator`` in production; the fallback path below only
        # runs in tests or ad-hoc scripts that construct the adapter directly.
        if self._cost_calculator is not None:
            return self._cost_calculator.calculate_llm_cost(
                model,
                prompt_tokens,
                completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )

        # Fall back to LiteLLM's built-in cost calculation.
        try:
            cost = litellm.completion_cost(completion_response=response)
            return round(cost, 6)
        except Exception as exc:
            # Silently returning 0 here previously masked the fact that
            # LiteLLM's pricing table does not know about every production
            # model (Anthropic 4.x landed without an entry for months). Log
            # loudly so the dataset operator spots the miscalculation.
            logger.warning(
                "litellm.completion_cost failed for model %r (%s); "
                "recording cost_usd=0.0. Inject a CostCalculatorPort "
                "(e.g. DefaultCostCalculator) to avoid this fallback.",
                model,
                exc,
            )
            return 0.0
