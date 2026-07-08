"""OpenRouter adapter: unified LLM gateway via the OpenAI-compatible API.

OpenRouter (https://openrouter.ai) proxies 100+ models from many providers
behind a single OpenAI-compatible Chat Completions endpoint. This adapter
uses the official ``openai`` SDK with ``base_url`` overridden, so one auth
key, one billing dashboard, and one client cover every model OpenRouter
exposes.

Model identifiers follow OpenRouter conventions, e.g.
``openai/gpt-4o-mini``, ``anthropic/claude-haiku-4.5``,
``google/gemini-2.5-flash``, ``meta-llama/llama-3.3-70b``.

The shared Qwen-style content-tool-call parsing and Anthropic "no tools"
message-cleanup helpers are reused from :mod:`content_tool_calls` and
:mod:`anthropic_cache`; they apply regardless of which gateway routes the
request.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

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
    apply_anthropic_cache_to_tools,
    strip_tool_content,
)
from infrastructure.adapters.content_tool_calls import try_parse_content_tool_calls
from infrastructure.adapters.llm_common import (
    extract_cache_tokens,
    is_o_series,
    require_model,
)
from infrastructure.io import robust_call


logger = logging.getLogger(__name__)


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter(LLMPort):
    """Query LLMs through OpenRouter via the official ``openai`` SDK.

    OpenRouter exposes an OpenAI-compatible chat-completions API, so the same
    SDK is reused: only the ``base_url`` and auth key differ. ``max_retries=0``
    is forced so 429s surface immediately as ``RateLimitError`` instead of
    being absorbed by the SDK's internal retry; the local retry policy below
    decides whether to retry with proper jittered backoff.
    """

    _LLM_TIMEOUT_SECONDS = 120
    _RATE_LIMIT_RETRIES = 1
    _RATE_LIMIT_BASE_DELAY = 15  # seconds; doubles each retry

    def __init__(
        self,
        default_config: dict[str, Any] | None = None,
        cost_calculator: CostCalculatorPort | None = None,
        *,
        api_key: str | None = None,
        base_url: str = _OPENROUTER_BASE_URL,
        referer: str | None = None,
        app_title: str | None = None,
    ) -> None:
        """Initialize the OpenRouter adapter.

        Args:
            default_config: Default LLM configuration (model, temperature, ...).
            cost_calculator: Optional cost calculator. If absent, the cost
                returned by OpenRouter in ``usage.cost`` (extra_body
                ``usage.include=true``) is used; if neither is available the
                cost is reported as 0.0.
            api_key: Override for ``OPENROUTER_API_KEY``. The env var is the
                normal source so deployments don't need code changes.
            base_url: OpenRouter endpoint. Override only for self-hosted or
                test doubles.
            referer / app_title: Optional headers OpenRouter uses for the
                dashboard's per-app attribution. Not required.
        """
        self.default_config = default_config or {}
        self._cost_calculator = cost_calculator
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENROUTER_API_KEY env var (or api_key constructor arg) is required"
            )
        headers: dict[str, str] = {}
        if referer:
            headers["HTTP-Referer"] = referer
        if app_title:
            headers["X-Title"] = app_title
        self._client = AsyncOpenAI(
            api_key=key,
            base_url=base_url,
            max_retries=0,
            timeout=self._LLM_TIMEOUT_SECONDS,
            default_headers=headers or None,
        )

    async def _call(self, *, model: str, **kwargs: Any) -> Any:
        """Call the OpenRouter chat-completions endpoint with timeout + retry.

        Retries only on ``RateLimitError`` and 5xx ``APIStatusError`` via
        ``robust_call.run``. Other errors (auth, bad request, validation)
        surface immediately as ``LLMError`` with the original exception
        chained on ``original_error``.
        """
        # Retry only on transient errors: 429s, 5xxs, network failures.
        # APIStatusError covers all HTTP errors so listing it would retry 401/
        # 403/400 too — bad. Pick the specific subclasses we want to retry.
        policy = robust_call.RetryPolicy(
            max_retries=self._RATE_LIMIT_RETRIES,
            base_delay=self._RATE_LIMIT_BASE_DELAY,
            jitter_range=(0.5, 1.5),
            backoff_base=2.0,
            retryable=(RateLimitError, InternalServerError, APIConnectionError),
        )

        def _log_retry(attempt: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "OpenRouter retry for %s (attempt %d/%d, %s), sleeping %.1fs",
                model,
                attempt + 1,
                self._RATE_LIMIT_RETRIES + 1,
                type(exc).__name__,
                delay,
            )

        try:
            return await robust_call.run(
                lambda: self._client.chat.completions.create(model=model, **kwargs),
                timeout=self._LLM_TIMEOUT_SECONDS,
                policy=policy,
                on_retry=_log_retry,
            )
        except TimeoutError as e:
            raise LLMError(
                f"OpenRouter request timed out after {self._LLM_TIMEOUT_SECONDS}s "
                f"for model '{model}'",
                original_error=e,
            ) from e
        except RateLimitError as e:
            raise LLMError(
                f"OpenRouter rate-limited on '{model}' after "
                f"{self._RATE_LIMIT_RETRIES + 1} attempts: {e}",
                original_error=e,
            ) from e
        except AuthenticationError as e:
            raise LLMError(
                f"OpenRouter authentication failed for '{model}': {e}",
                original_error=e,
            ) from e
        except APITimeoutError as e:
            raise LLMError(
                f"OpenRouter HTTP timeout for '{model}': {e}",
                original_error=e,
            ) from e
        except APIStatusError as e:
            raise LLMError(
                f"OpenRouter API error for '{model}' (status={e.status_code}): {e}",
                original_error=e,
            ) from e
        except APIConnectionError as e:
            raise LLMError(
                f"OpenRouter connection error for '{model}': {e}",
                original_error=e,
            ) from e

    def _build_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        merged_config: dict[str, Any],
        model: str,
        default_max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Assemble the keyword arguments for one chat-completions call."""
        kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": merged_config.get("max_tokens", default_max_tokens),
        }
        top_p = merged_config.get("top_p")
        if top_p is not None:
            kwargs["top_p"] = top_p
        if not is_o_series(model):
            kwargs["temperature"] = merged_config.get("temperature", 0.7)
        if tools:
            kwargs["tools"] = tools
        # Ask OpenRouter to include accurate per-call cost in the response so
        # we don't depend on a separate price registry to track spend.
        kwargs["extra_body"] = {"usage": {"include": True}}
        return kwargs

    @staticmethod
    def _extract_usage(response: Any) -> LLMUsage:
        usage = getattr(response, "usage", None)
        if usage is None:
            return LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        cache_read_tokens, cache_write_tokens = extract_cache_tokens(usage)
        return LLMUsage(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

    def _extract_cost(
        self,
        response: Any,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Pick a cost source in this order: OpenRouter response, calculator, 0."""
        usage = getattr(response, "usage", None)
        if usage is not None:
            reported = getattr(usage, "cost", None)
            if reported is not None:
                try:
                    return round(float(reported), 6)
                except (TypeError, ValueError):
                    logger.debug("OpenRouter usage.cost not numeric: %r", reported)
        if self._cost_calculator is not None:
            return self._cost_calculator.calculate_llm_cost(
                model, prompt_tokens, completion_tokens
            )
        return 0.0

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        merged_config = {**self.default_config, **config_dict}
        model = require_model(merged_config)
        kwargs = self._build_kwargs(
            messages=apply_anthropic_cache_to_messages(
                [{"role": "user", "content": prompt}], model
            ),
            merged_config=merged_config,
            model=model,
            default_max_tokens=1000,
        )
        response = await self._call(model=model, **kwargs)
        content = response.choices[0].message.content
        if content is None:
            raise LLMError("OpenRouter returned empty response")
        return content

    async def query_with_usage(
        self, prompt: str, config_dict: dict[str, Any]
    ) -> LLMResponse:
        merged_config = {**self.default_config, **config_dict}
        model = require_model(merged_config)
        kwargs = self._build_kwargs(
            messages=apply_anthropic_cache_to_messages(
                [{"role": "user", "content": prompt}], model
            ),
            merged_config=merged_config,
            model=model,
            default_max_tokens=1000,
        )
        response = await self._call(model=model, **kwargs)
        content = response.choices[0].message.content
        if content is None:
            raise LLMError("OpenRouter returned empty response")
        usage = self._extract_usage(response)
        cost = self._extract_cost(
            response, model, usage.prompt_tokens, usage.completion_tokens
        )
        return LLMResponse(content=content, usage=usage, model=model, cost_usd=cost)

    async def query_with_tools(
        self,
        messages: list[dict[str, Any]],
        config_dict: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        merged_config = {**self.default_config, **config_dict}
        model = require_model(merged_config)
        # Anthropic rejects messages referencing tool_calls when no tools are
        # defined. Strip tool turns so the post-iteration "force final answer"
        # call works for both Anthropic-via-OpenRouter and OpenAI-via-OpenRouter.
        outbound_messages = messages if tools else strip_tool_content(messages)
        outbound_messages = apply_anthropic_cache_to_messages(outbound_messages, model)
        kwargs = self._build_kwargs(
            messages=outbound_messages,
            merged_config=merged_config,
            model=model,
            default_max_tokens=4000,
            tools=apply_anthropic_cache_to_tools(tools or None, model),
        )
        response = await self._call(model=model, **kwargs)
        message = response.choices[0].message
        content = message.content
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json.loads(args) if args else {}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )
        elif tools and content:
            # Qwen-family models (routable via OpenRouter) sometimes emit tool
            # invocations as plain JSON in message.content. Detect and parse.
            parsed = try_parse_content_tool_calls(content, tools)
            if parsed:
                tool_calls.extend(parsed)
                content = None
        usage = self._extract_usage(response)
        cost = self._extract_cost(
            response, model, usage.prompt_tokens, usage.completion_tokens
        )
        return LLMToolResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            cost_usd=cost,
        )
