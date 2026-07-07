"""LiteLLM adapter: unified interface to multiple LLM providers.

This adapter wraps LiteLLM to provide a unified interface for querying
various LLM providers (OpenAI, Anthropic, Google, etc.) with optional
cost tracking via the CostCalculatorPort.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import litellm

from core.domain.exceptions import LLMError
from core.domain.values.llm_response import (
    LLMResponse,
    LLMToolResponse,
    LLMUsage,
    ToolCall,
)
from core.ports.runtime_ports import CostCalculatorPort, LLMPort
from infrastructure.adapters.anthropic_cache import (
    apply_anthropic_cache_to_messages,
    apply_anthropic_cache_to_tail,
    apply_anthropic_cache_to_tools,
    strip_tool_content,
)
from infrastructure.adapters.content_tool_calls import try_parse_content_tool_calls
from infrastructure.io import robust_call


logger = logging.getLogger(__name__)


def require_model(merged_config: dict[str, Any]) -> str:
    model = merged_config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise LLMError("LLM model must be configured explicitly")
    return model


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
        base = model.split("/")[-1].lower()
        return base.startswith(("o1", "o3", "o4", "gpt-5"))

    @staticmethod
    def _provider_overrides(merged_config: dict[str, Any]) -> dict[str, Any]:
        """Extract optional LiteLLM overrides (api_base, api_key) from config.

        Values that are None are omitted so LiteLLM's own env-var resolution
        (e.g. OLLAMA_API_BASE, OLLAMA_API_KEY) still applies.
        """
        overrides: dict[str, Any] = {}
        for key in ("api_base", "api_key"):
            value = merged_config.get(key)
            if value:
                overrides[key] = value
        return overrides

    @staticmethod
    def _ollama_overrides(model: str) -> dict[str, Any]:
        """Return extra kwargs for Ollama models.

        Applies two critical overrides for Ollama reasoning models
        (qwen3.5, deepseek-v4-flash, etc.):

        1. **Disable thinking mode** — these models emit reasoning as
           inline ``<think>`` tags (Qwen) or in a separate ``thinking``
           field (DeepSeek), often leaving ``content`` empty on long
           structured prompts.

        2. **Set num_ctx** — Ollama defaults to num_ctx=2048 which
           truncates prompts longer than ~1500 tokens. Our decomposition
           prompts are 6-8K tokens; without explicit num_ctx the model
           cannot see domain-specific rules (e.g. "Builder MUST decompose")
           buried later in the prompt, causing systematic failures.

        litellm's ``reasoning_effort`` param does NOT propagate to Ollama.
        """
        if not model.startswith(("ollama/", "ollama_chat/")):
            return {}
        # litellm's ollama_chat handler pops top-level ``think`` out of
        # optional_params and writes it into the request body as
        # ``data["think"]`` (see litellm/llms/ollama_chat.py). Passing it
        # as a direct kwarg avoids the extra_body merge logic, which
        # behaves differently across providers.
        return {
            "think": False,
            "num_ctx": 65536,
        }

    @staticmethod
    def _maybe_prompt_cache_key(merged_config: dict[str, Any], model: str) -> dict[str, Any]:
        """OpenAI prompt-cache routing key (manager-cache fix).

        Emits ``prompt_cache_key`` (a shared per-run key injected by the orchestrator) for
        OpenAI models only, so serialized sibling boss/manager calls route to one cache and
        reuse their large shared prefix. Anthropic uses ``cache_control``; Ollama ignores it.
        """
        key = merged_config.get("prompt_cache_key")
        base = model.split("/")[-1].lower()
        is_openai = base.startswith(("gpt-", "o1", "o3", "o4")) and "claude" not in model.lower()
        return {"prompt_cache_key": key} if key and is_openai else {}

    # Per-attempt hard timeout. Sized above the ~120s that a large cache-WRITE
    # seeding request (the rolling tool-loop tail on Sonnet) can take under
    # parallel load; the 900s agent-step watchdog still bounds the whole step.
    _LLM_TIMEOUT_SECONDS = 240
    # Cap retries low because retries x timeout x tool-iterations could compound
    # into minutes of DB-silent retry. One retry with a cooldown clears a
    # transient timeout / 429 spike (a fresh request usually routes around the
    # congestion) without long compounding; the agent-step watchdog backstops.
    _RATE_LIMIT_RETRIES = 1
    _RATE_LIMIT_BASE_DELAY = 15  # seconds; doubles each retry

    async def _call_litellm(self, model: str, **kwargs: Any) -> Any:
        """Call litellm.acompletion with timeout, 429 retry, and exception handling.

        Retries rate-limit and service-unavailable errors via
        ``robust_call.run`` with jittered exponential backoff. Applies a
        hard per-attempt timeout of ``_LLM_TIMEOUT_SECONDS`` to prevent
        indefinite hangs when the endpoint is overwhelmed.

        Args:
            model: Model identifier (e.g., 'gpt-4', 'claude-3-opus').
            **kwargs: Passed directly to litellm.acompletion (messages, tools, etc.).

        Returns:
            Raw LiteLLM response object.

        Raises:
            LLMError: On any API failure, with specific error messages.
        """
        policy = robust_call.RetryPolicy(
            max_retries=self._RATE_LIMIT_RETRIES,
            base_delay=self._RATE_LIMIT_BASE_DELAY,
            jitter_range=(0.5, 1.5),
            backoff_base=2.0,
            retryable=(
                litellm.exceptions.RateLimitError,
                litellm.exceptions.ServiceUnavailableError,
                # A per-attempt timeout is usually a transient latency spike
                # (busy endpoint); a fresh retry routes around it. robust_call
                # retries asyncio.wait_for's TimeoutError only when listed here.
                asyncio.TimeoutError,
            ),
        )

        def _log_retry(attempt: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "Transient LLM error from %s (%s, attempt %d/%d), retrying in %.1fs",
                model,
                type(exc).__name__,
                attempt + 1,
                self._RATE_LIMIT_RETRIES + 1,
                delay,
            )

        try:
            return await robust_call.run(
                lambda: litellm.acompletion(model=model, **kwargs),
                timeout=self._LLM_TIMEOUT_SECONDS,
                policy=policy,
                on_retry=_log_retry,
            )
        except asyncio.TimeoutError as e:
            raise LLMError(
                f"LLM request timed out after {self._LLM_TIMEOUT_SECONDS}s for model '{model}'",
                original_error=e,
            ) from e
        except (
            litellm.exceptions.RateLimitError,
            litellm.exceptions.ServiceUnavailableError,
        ) as e:
            raise LLMError(
                f"LLM rate limit exceeded for model '{model}' after "
                f"{self._RATE_LIMIT_RETRIES + 1} attempts: {e}",
                original_error=e,
            ) from e
        except litellm.exceptions.AuthenticationError as e:
            raise LLMError(
                f"LLM authentication failed for model '{model}': {e}",
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
        except Exception as e:
            raise LLMError(
                f"Unexpected LLM error for model '{model}': {e}",
                original_error=e,
            ) from e

    def _extract_usage(self, model: str, response: Any) -> tuple[LLMUsage, float]:
        """Extract token usage and cost from a raw LiteLLM response.

        Returns:
            Tuple of (LLMUsage, cost_usd).
        """
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else 0
        cache_read_tokens, cache_write_tokens = self._extract_cache_tokens(usage)
        cost_usd = self._calculate_cost(model, prompt_tokens, completion_tokens, response)

        return (
            LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            cost_usd,
        )

    @staticmethod
    def _extract_cache_tokens(raw_usage: Any) -> tuple[int, int]:
        """Extract prompt-cache read/write tokens from a LiteLLM usage object.

        LiteLLM normalizes Anthropic cache stats onto the usage object as
        cache_read_input_tokens (cache hit) and cache_creation_input_tokens
        (cache write). OpenAI-style responses nest the read count under
        prompt_tokens_details.cached_tokens, used here as a read fallback.
        Defensive getattr keeps non-caching providers (no such fields) at 0.
        """
        if raw_usage is None:
            return 0, 0
        cache_read_tokens = getattr(raw_usage, "cache_read_input_tokens", 0) or 0
        cache_write_tokens = getattr(raw_usage, "cache_creation_input_tokens", 0) or 0
        if not cache_read_tokens:
            details = getattr(raw_usage, "prompt_tokens_details", None)
            if details is not None:
                cache_read_tokens = getattr(details, "cached_tokens", 0) or 0
        return cache_read_tokens, cache_write_tokens

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
        model = require_model(merged_config)

        call_kwargs: dict[str, Any] = {
            "messages": apply_anthropic_cache_to_messages(
                [{"role": "user", "content": prompt}], model
            ),
            "max_tokens": merged_config.get("max_tokens", 1000),
            "top_p": merged_config.get("top_p"),
        }
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)
        call_kwargs.update(self._provider_overrides(merged_config))
        call_kwargs.update(self._maybe_prompt_cache_key(merged_config, model))
        call_kwargs.update(self._ollama_overrides(model))

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
        model = require_model(merged_config)

        call_kwargs: dict[str, Any] = {
            "messages": apply_anthropic_cache_to_messages(
                [{"role": "user", "content": prompt}], model
            ),
            "max_tokens": merged_config.get("max_tokens", 1000),
            "top_p": merged_config.get("top_p"),
        }
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)
        call_kwargs.update(self._provider_overrides(merged_config))
        call_kwargs.update(self._maybe_prompt_cache_key(merged_config, model))
        call_kwargs.update(self._ollama_overrides(model))

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
        model = require_model(merged_config)

        call_kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": merged_config.get("max_tokens", 4000),
            "top_p": merged_config.get("top_p"),
        }
        if tools:
            call_kwargs["tools"] = apply_anthropic_cache_to_tools(tools, model)
        else:
            # Anthropic rejects messages containing tool_calls/tool results
            # when no tools are defined. Strip tool content so the fallback
            # "force final answer" call after max iterations works cleanly.
            call_kwargs["messages"] = strip_tool_content(messages)
        call_kwargs["messages"] = apply_anthropic_cache_to_messages(
            call_kwargs["messages"], model
        )
        call_kwargs["messages"] = apply_anthropic_cache_to_tail(
            call_kwargs["messages"], model
        )
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)
        call_kwargs.update(self._provider_overrides(merged_config))
        call_kwargs.update(self._maybe_prompt_cache_key(merged_config, model))
        call_kwargs.update(self._ollama_overrides(model))

        response = await self._call_litellm(model=model, **call_kwargs)

        message = response.choices[0].message
        content = message.content

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
        elif tools and content:
            # Qwen-family models (e.g. qwen3.5 via Ollama) may emit tool calls
            # as plain JSON in message.content instead of populating
            # message.tool_calls.  Detect and parse these so the tool-calling
            # loop can execute them.  Only activates when: (1) the API was
            # given tool definitions, (2) no structured tool_calls came back,
            # and (3) content parses as tool-call-shaped JSON.  GPT/Claude
            # models always populate message.tool_calls, so this path is
            # never reached for them.
            parsed_tcs = try_parse_content_tool_calls(content, tools)
            if parsed_tcs:
                tool_calls.extend(parsed_tcs)
                content = None  # consumed as tool call, not text

        llm_usage, cost_usd = self._extract_usage(model, response)

        return LLMToolResponse(
            content=content,
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
    ) -> float:
        """Calculate cost using injected calculator or LiteLLM fallback.

        Args:
            model: Model name.
            prompt_tokens: Number of input tokens.
            completion_tokens: Number of output tokens.
            response: Raw LiteLLM response (for built-in cost calculation).

        Returns:
            Cost in USD.
        """
        # Use injected calculator if available
        if self._cost_calculator is not None:
            return self._cost_calculator.calculate_llm_cost(model, prompt_tokens, completion_tokens)

        # Fall back to LiteLLM's built-in cost calculation
        try:
            cost = litellm.completion_cost(completion_response=response)
            return round(cost, 6)
        except Exception:
            # If LiteLLM cost calculation fails, return 0
            return 0.0
