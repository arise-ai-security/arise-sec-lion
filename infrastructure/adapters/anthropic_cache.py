"""Anthropic prompt-cache decoration and message compatibility for LLM adapters.

Pure message/tool transformations shared by the LiteLLM and OpenRouter
adapters: gate ``cache_control`` emission to Anthropic-bound requests and
sanitize tool content for Anthropic's no-tools calls.
"""

from __future__ import annotations

from typing import Any

from core.application.services.prompt.cache_breakpoint import (
    split_cache_breakpoint,
    strip_cache_breakpoint,
)


def strip_tool_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool_calls and tool-result messages from a conversation history.

    Anthropic rejects messages that reference tools (assistant tool_calls or
    role=tool results) when no ``tools`` parameter is provided. This is used
    by the fallback "force final answer" path after max tool iterations.
    """
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            continue
        out_msg = msg
        if out_msg.get("tool_calls"):
            out_msg = {k: v for k, v in out_msg.items() if k != "tool_calls"}
            if not out_msg.get("content"):
                out_msg["content"] = ""
        cleaned.append(out_msg)
    return cleaned


def supports_anthropic_cache(model: str) -> bool:
    # Anthropic prompt caching (5-min ephemeral TTL, ~90% read discount)
    # only applies when the request reaches an Anthropic model. LiteLLM
    # passes `cache_control` through on these models and ignores/rejects it
    # elsewhere, so we gate emission defensively.
    m = model.lower()
    return "claude" in m or m.startswith(("anthropic/", "bedrock/anthropic."))


def apply_anthropic_cache_to_messages(
    messages: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    # Prepare the first user message for prompt caching.
    # - With a cache-breakpoint marker: split into a cacheable static prefix
    #   (cache_control) + variable tail (Anthropic), or strip the marker to
    #   plain text (non-Anthropic) so it never reaches the model.
    # - Without a marker on an Anthropic model: cache the whole first user
    #   message (prior behavior). Non-Anthropic + no marker: unchanged.
    supports = supports_anthropic_cache(model)
    out = list(messages)
    for i, msg in enumerate(out):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            content = msg["content"]
            split = split_cache_breakpoint(content)
            if split is not None:
                static, variable = split
                if supports:
                    blocks: list[dict[str, Any]] = [
                        {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}}
                    ]
                    if variable:
                        # Restore the "\n\n" seam that split_cache_breakpoint
                        # trimmed, so the two content blocks concatenate to the
                        # exact bytes of the non-cached path (static + "\n\n" +
                        # variable) — Anthropic joins text blocks with no
                        # separator. The static (cached) block stays clean.
                        blocks.append({"type": "text", "text": f"\n\n{variable}"})
                    out[i] = {**msg, "content": blocks}
                else:
                    out[i] = {**msg, "content": strip_cache_breakpoint(content)}
            elif supports:
                out[i] = {
                    **msg,
                    "content": [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ],
                }
            break
    return out


def apply_anthropic_cache_to_tail(
    messages: list[dict[str, Any]], model: str
) -> list[dict[str, Any]]:
    # Roll an ephemeral cache breakpoint onto the LAST message so the growing
    # tool-loop tail (assistant tool_calls + tool results that accumulate each
    # recon round) is served as cache_read on the next round instead of full
    # price. This is a SEPARATE pass from apply_anthropic_cache_to_messages,
    # which only marks the first user message and returns early — it never
    # reaches the tail. Breakpoint budget: tools(1) + first-user(1) + tail(1) =
    # 3 of Anthropic's 4. The tail only pays off because the loop now keeps the
    # prefix byte-stable (elide-once + read dedup), so the cached region is
    # actually re-read rather than re-seeded.
    if not supports_anthropic_cache(model) or len(messages) <= 1:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = [dict(b) for b in content]
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = blocks
    else:
        return messages
    out[-1] = last
    return out


def apply_anthropic_cache_to_tools(
    tools: list[dict[str, Any]] | None, model: str
) -> list[dict[str, Any]] | None:
    # A single marker on the last tool entry tells Anthropic to cache the
    # entire tools array. The tool-calling loop re-sends the full array on
    # every turn; without this, every turn pays full input on identical
    # tool schemas.
    if not tools or not supports_anthropic_cache(model):
        return tools
    out = list(tools)
    last = dict(out[-1])
    last["cache_control"] = {"type": "ephemeral"}
    out[-1] = last
    return out
