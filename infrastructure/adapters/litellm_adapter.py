"""LiteLLM adapter: unified interface to multiple LLM providers.

This adapter wraps LiteLLM to provide a unified interface for querying
various LLM providers (OpenAI, Anthropic, Google, etc.) with optional
cost tracking via the CostCalculatorPort.
"""

import json
import logging
import uuid
from typing import Any

import litellm

from core.domain.exceptions import LLMError
from core.domain.values.llm_response import LLMResponse, LLMToolResponse, LLMUsage, ToolCall
from core.ports.runtime_ports import CostCalculatorPort, LLMPort

logger = logging.getLogger(__name__)


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
        """Return True for OpenAI models that reject temperature (O-series, GPT-5)."""
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

        Returns:
            Tuple of (LLMUsage, cost_usd).
        """
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else 0
        cost_usd = self._calculate_cost(model, prompt_tokens, completion_tokens, response)

        return (
            LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
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
        call_kwargs.update(self._provider_overrides(merged_config))

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
        call_kwargs.update(self._provider_overrides(merged_config))

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
        call_kwargs.update(self._provider_overrides(merged_config))

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
            # and (3) content parses as a tool-call-shaped dict.  GPT/Claude
            # models always populate message.tool_calls, so this path is
            # never reached for them.
            parsed_tc = _try_parse_content_tool_call(content, tools)
            if parsed_tc is not None:
                tool_calls.append(parsed_tc)
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


def _try_parse_content_tool_call(
    content: str,
    tools: list[dict[str, Any]],
) -> ToolCall | None:
    """Detect a tool call emitted as plain JSON in message.content.

    Qwen-family models (e.g. qwen3.5 via Ollama Cloud) sometimes return
    tool invocations as raw JSON text instead of populating the API's
    ``tool_calls`` field.  Two known shapes:

    1. ``{"name": "<tool>", "arguments": {...}}``  — OpenAI-style wrapper
    2. ``{"<param>": <value>, ...}``               — bare arguments dict

    For shape (2) we match against the registered tool definitions: if the
    dict keys are a subset of exactly one tool's parameters, treat it as a
    call to that tool.

    Returns ``None`` when the content is not recognisable as a tool call,
    so callers treat the response as a normal text answer (no behaviour
    change for models that already use structured tool_calls).
    """
    stripped = content.strip()
    if not stripped.startswith("{"):
        return None

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # Shape 1: {"name": "...", "arguments": {...}}
    if "name" in data and "arguments" in data and isinstance(data["arguments"], dict):
        tool_name = data["name"]
        if any(
            t.get("function", {}).get("name") == tool_name
            for t in tools
        ):
            logger.info("Parsed Qwen-style text tool call: %s", tool_name)
            return ToolCall(
                id=f"qwen-text-{uuid.uuid4().hex[:8]}",
                name=tool_name,
                arguments=data["arguments"],
            )

    # Shape 2: bare arguments dict — match keys against tool parameter schemas
    tool_names = _build_tool_param_index(tools)
    keys = frozenset(data.keys())
    candidates = [name for name, params in tool_names.items() if keys <= params]
    if len(candidates) == 1:
        tool_name = candidates[0]
        logger.info(
            "Parsed Qwen-style bare-args tool call: %s (keys=%s)",
            tool_name, sorted(keys),
        )
        return ToolCall(
            id=f"qwen-text-{uuid.uuid4().hex[:8]}",
            name=tool_name,
            arguments=data,
        )

    return None


def _build_tool_param_index(
    tools: list[dict[str, Any]],
) -> dict[str, frozenset[str]]:
    """Build tool-name → parameter-keys index from OpenAI-format tool defs."""
    index: dict[str, frozenset[str]] = {}
    for t in tools:
        func = t.get("function", {})
        name = func.get("name")
        if not name:
            continue
        params = func.get("parameters", {}).get("properties", {})
        index[name] = frozenset(params.keys())
    return index
