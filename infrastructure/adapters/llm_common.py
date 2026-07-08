"""Helpers shared by the LLM gateway adapters (LiteLLM, OpenRouter)."""

from __future__ import annotations

from typing import Any

from core.domain.exceptions import LLMError


def require_model(merged_config: dict[str, Any]) -> str:
    model = merged_config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise LLMError("LLM model must be configured explicitly")
    return model


def is_o_series(model: str) -> bool:
    """True for OpenAI reasoning models that reject ``temperature``.

    The suffix check works for both direct ids (``gpt-5-...``) and
    gateway-prefixed ids (``openai/o3-...``).
    """
    base = model.split("/")[-1].lower()
    return base.startswith(("o1", "o3", "o4", "gpt-5"))


def extract_cache_tokens(raw_usage: Any) -> tuple[int, int]:
    """Extract prompt-cache read/write tokens from a chat-completions usage object.

    Anthropic-normalized responses expose cache_read_input_tokens (cache hit)
    and cache_creation_input_tokens (cache write). OpenAI-style responses nest
    the read count under prompt_tokens_details.cached_tokens, used here as a
    read fallback. Defensive getattr keeps non-caching providers at 0.
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
