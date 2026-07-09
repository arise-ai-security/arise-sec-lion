"""Detect tool calls emitted as plain text/JSON in LLM message content.

Some models served through Ollama/OpenRouter (Qwen family, DeepSeek) return
tool invocations inside ``message.content`` instead of the API's structured
``tool_calls`` field. This module recognizes those shapes and converts them
to ``ToolCall`` values; GPT/Claude responses never reach this path.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from core.domain.values.llm_response import ToolCall


logger = logging.getLogger(__name__)

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


def try_parse_content_tool_calls(  # noqa: PLR0911, PLR0912
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

    tool_name_set = {t.get("function", {}).get("name") for t in tools}
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
    candidates = [name for name, params in tool_param_index.items() if keys <= params]
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
    index: dict[str, frozenset[str]] = {}
    for t in tools:
        func = t.get("function", {})
        name = func.get("name")
        if not name:
            continue
        params = func.get("parameters", {}).get("properties", {})
        index[name] = frozenset(params.keys())
    return index
