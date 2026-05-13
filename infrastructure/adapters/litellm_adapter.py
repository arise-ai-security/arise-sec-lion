"""LiteLLM adapter: unified interface to multiple LLM providers.

This adapter wraps LiteLLM to provide a unified interface for querying
various LLM providers (OpenAI, Anthropic, Google, etc.) with optional
cost tracking via the CostCalculatorPort.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
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
from infrastructure.io import robust_call


logger = logging.getLogger(__name__)


def _strip_tool_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool_calls and tool-result messages from a conversation history.

    Anthropic rejects messages that reference tools (assistant tool_calls or
    role=tool results) when no ``tools`` parameter is provided. This is used
    by the fallback "force final answer" path after max tool iterations.
    """
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            continue
        if msg.get("tool_calls"):
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            if not msg.get("content"):
                msg["content"] = ""
        cleaned.append(msg)
    return cleaned


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
    def _is_ollama(model: str) -> bool:
        """Return True for models served via Ollama (local or cloud)."""
        return model.startswith(("ollama/", "ollama_chat/"))

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

    _LLM_TIMEOUT_SECONDS = 120  # Hard timeout for a single LLM call
    # Cap retries low because 3 retries x 180s x 5 tool-iterations could
    # compound to ~25 min of DB-silent retry. One retry with a longer cooldown
    # propagates a 429 storm as an LLMError quickly enough that the per-step
    # timeout (default 600s) can recycle the agent.
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
            ),
        )

        def _log_retry(attempt: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "Rate limited by %s (attempt %d/%d), retrying in %.1fs",
                model,
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
        model = merged_config.get("model", "gpt-4")

        call_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": merged_config.get("max_tokens", 1000),
            "top_p": merged_config.get("top_p"),
        }
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)
        call_kwargs.update(self._provider_overrides(merged_config))
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
        model = merged_config.get("model", "gpt-4")

        call_kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": merged_config.get("max_tokens", 4000),
            "top_p": merged_config.get("top_p"),
        }
        if tools:
            call_kwargs["tools"] = tools
        else:
            # Anthropic rejects messages containing tool_calls/tool results
            # when no tools are defined. Strip tool content so the fallback
            # "force final answer" call after max iterations works cleanly.
            call_kwargs["messages"] = _strip_tool_content(messages)
        if not self._is_o_series(model):
            call_kwargs["temperature"] = merged_config.get("temperature", 0.7)
        call_kwargs.update(self._provider_overrides(merged_config))
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
            parsed_tcs = _try_parse_content_tool_calls(content, tools)
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


_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_TAG = "<tool_call>"


def _strip_llm_wrappers(text: str) -> str:
    """Strip Qwen think tags and markdown code fences from LLM output.

    Same pre-processing as the subtask parser, applied here so that
    tool-call-shaped JSON wrapped in think tags or code fences is
    detected before it reaches downstream parsers.
    """
    cleaned = _THINK_TAG_RE.sub("", text).strip()
    match = _CODE_FENCE_RE.search(cleaned)
    if match:
        cleaned = match.group(1).strip()
    return cleaned


def _extract_tool_call_tags(content: str) -> list[dict[str, Any]]:
    """Extract parsed JSON objects from ``<tool_call>`` XML tags.

    DeepSeek-v4-pro (and some other Ollama-served models) emit tool calls
    as ``<tool_call>{"name": "…", "arguments": {…}}</tool_call>`` tags
    embedded in conversational prose, rather than populating the API's
    structured ``tool_calls`` field.  Uses ``json.JSONDecoder.raw_decode``
    to find each JSON object robustly (handles nested braces).
    """
    if _TOOL_CALL_TAG not in content.lower():
        return []
    results: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    lower = content.lower()
    pos = 0
    while True:
        idx = lower.find(_TOOL_CALL_TAG, pos)
        if idx == -1:
            break
        json_start = idx + len(_TOOL_CALL_TAG)
        # Skip whitespace between tag and JSON
        while json_start < len(content) and content[json_start] in " \t\n\r":
            json_start += 1
        if json_start >= len(content) or content[json_start] != "{":
            pos = json_start
            continue
        try:
            obj, end = decoder.raw_decode(content, json_start)
            if isinstance(obj, dict):
                results.append(obj)
            pos = json_start + end
        except json.JSONDecodeError:
            pos = json_start + 1
    return results


def _try_parse_content_tool_calls(
    content: str,
    tools: list[dict[str, Any]],
) -> list[ToolCall] | None:
    """Detect tool calls emitted as plain JSON in message.content.

    Qwen-family models (e.g. qwen3.5 via Ollama Cloud) sometimes return
    tool invocations as raw JSON text instead of populating the API's
    ``tool_calls`` field.  Four known shapes:

    1.  ``{"name": "<tool>", "arguments": {...}}``  — OpenAI-style wrapper
    1b. ``{"name": "<tool>", ...}``                 — name matches a tool,
        remaining fields become arguments (no ``description`` key —
        that would indicate a subtask, not a tool call)
    2.  ``{"<param>": <value>, ...}``               — bare arguments dict
    3.  ``[<item>, ...]``                           — array of any of the
        above; all items must resolve to tool calls (if any item looks
        like a subtask — has a ``description`` field — we bail and let
        the subtask parser handle the whole array)
    4.  ``<tool_call>{"name": "…", …}</tool_call>`` — DeepSeek-style XML
        tag wrapping, often preceded by conversational prose.

    For shape (2) we match against the registered tool definitions: if the
    dict keys are a subset of exactly one tool's parameters, treat it as a
    call to that tool.

    Returns ``None`` when the content is not recognisable as tool calls,
    so callers treat the response as a normal text answer (no behaviour
    change for models that already use structured tool_calls).
    """
    # Strip Qwen <think>…</think> tags and markdown code fences before
    # checking for JSON.  The subtask parser does this too, but we need
    # it here so tool-call-shaped content wrapped in think tags is
    # intercepted before it reaches the subtask parser.
    stripped = _strip_llm_wrappers(content)

    # Shape 4: DeepSeek-v4-pro emits <tool_call>JSON</tool_call> tags
    # embedded in prose. Extract these before the JSON-start check,
    # since conversational preamble precedes the tag.
    tagged_items = _extract_tool_call_tags(stripped)
    if tagged_items:
        tool_name_set = {t.get("function", {}).get("name") for t in tools}
        tool_param_index = _build_tool_param_index(tools)
        calls: list[ToolCall] = []
        for item in tagged_items:
            tc = _match_single_tool_call(item, tool_name_set, tool_param_index)
            if tc is None:
                return None  # not a tool call — bail to subtask parser
            calls.append(tc)
        if calls:
            for tc in calls:
                logger.info("Parsed <tool_call>-tagged tool call: %s", tc.name)
            return calls

    if not stripped.startswith(("{", "[")):
        return None

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    # Normalize to a list of items to inspect
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list) and data:
        items = [x for x in data if isinstance(x, dict)]
        if not items:
            return None
    else:
        return None

    tool_name_set = {
        t.get("function", {}).get("name")
        for t in tools
    }
    tool_param_index = _build_tool_param_index(tools)

    calls: list[ToolCall] = []
    for item in items:
        tc = _match_single_tool_call(item, tool_name_set, tool_param_index)
        if tc is None:
            # If any item is not a tool call, the whole blob might be
            # subtask JSON — bail and let the subtask parser handle it.
            return None
        calls.append(tc)

    if calls:
        for tc in calls:
            logger.info("Parsed Qwen-style text tool call: %s", tc.name)
    return calls or None


def _match_single_tool_call(
    item: dict[str, Any],
    tool_name_set: set[str | None],
    tool_param_index: dict[str, frozenset[str]],
) -> ToolCall | None:
    """Try to match a single dict as a tool call against known tool defs.

    Returns a ToolCall on match, None otherwise.
    """
    # Shape 1: {"name": "<tool>", "arguments": {...}}
    if "name" in item and "arguments" in item and isinstance(item["arguments"], dict):
        name = item["name"]
        if isinstance(name, str) and name in tool_name_set:
            return ToolCall(
                id=f"qwen-text-{uuid.uuid4().hex[:8]}",
                name=name,
                arguments=item["arguments"],
            )

    # Shape 1b: {"name": "<tool>", ...} — name matches a tool, no
    # "arguments" wrapper, and no "description" (which would indicate
    # a subtask rather than a tool call).
    name = item.get("name")
    if (
        isinstance(name, str)
        and name in tool_name_set
        and "description" not in item
        and "arguments" not in item
    ):
        args = {k: v for k, v in item.items() if k != "name"}
        return ToolCall(
            id=f"qwen-text-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments=args,
        )

    # Shape 2: bare arguments dict — keys subset of exactly one tool
    keys = frozenset(item.keys())
    candidates = [
        name for name, params in tool_param_index.items()
        if keys <= params
    ]
    if len(candidates) == 1:
        return ToolCall(
            id=f"qwen-text-{uuid.uuid4().hex[:8]}",
            name=candidates[0],
            arguments=item,
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
