"""LLM-backed fallback parser that repairs malformed model output.

When the deterministic JSON-extraction pipeline (``raw_decode`` →
``_repair_json``) fails on output from a less-disciplined model
(qwen3, qwen3-coder, deepseek, GLM, …), this adapter forwards the raw
text to a small/fast repair model with a strict "preserve every value
verbatim, only fix structure" instruction. The repaired response is
re-fed through the standard parser by the caller.

Naming note: this repairs the *output format* of LLM responses. It is
unrelated to the security-domain "Fixer" agent role, which patches
source code.

Design notes:
- The repair model is itself an LLM, so it is selectable via config.
  It must be configured explicitly by experiments that enable repair.
- One shot, no internal retries. If the repair call fails or its output
  is also malformed, the caller surfaces the original parse error.
- We do not attempt to verify "values were preserved" — that would
  require parsing both inputs, and the repair we are doing is exactly
  that the original cannot be parsed. We rely on the prompt instruction
  and downstream pydantic validation.
- Concurrent repair calls are bounded by an ``asyncio.Semaphore`` sized
  via ``max_concurrent``. A parse-failure storm across parallel agents
  must not amplify into N simultaneous LLM requests against the repair
  endpoint (Ollama Cloud or local Ollama) — that would re-create the
  rate-limit pressure the repair path is meant to absorb.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.domain.exceptions import LLMError
from core.ports.runtime_ports import FormatRepairerPort, LLMPort


logger = logging.getLogger(__name__)


_REPAIR_PROMPT = """\
You are a strict output-format repair tool. The text below was emitted by
another language model that was asked to return JSON matching a specific
schema, but the output is structurally invalid (truncated braces, trailing
prose, think-tags, single quotes, missing commas, wrapped in tool-call
shape, etc.).

Your job:
1. Read the malformed output.
2. Return a SINGLE valid JSON value matching the target schema below.
3. Preserve EVERY field value from the original output verbatim. Do NOT
   summarize, paraphrase, translate, re-order, or invent content. If a
   string value contains characters that need escaping, escape them, but
   do not change what the string says.
4. If the original is missing a required field, OMIT it (do not invent
   defaults). Downstream validation will reject the result if needed.
5. Strip any prose, <think>...</think> blocks, markdown fences, or
   tool-call wrappers ({{"name": ..., "arguments": {{...}}}}) — return
   only the underlying JSON payload.
6. Output ONLY the JSON. No prose, no code fences, no commentary.
   Your entire response must parse with json.loads().

Target schema (informal):
{schema_hint}

Malformed output:
{raw}
"""


class LLMFormatRepairer(FormatRepairerPort):
    """Use an LLM to repair malformed JSON-shaped output."""

    def __init__(
        self,
        llm_port: LLMPort,
        model: str,
        max_tokens: int = 8000,
        api_base: str | None = None,
        max_concurrent: int = 3,
    ) -> None:
        self._llm_port = llm_port
        self._model = model
        self._max_tokens = max_tokens
        self._api_base = api_base
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def repair(self, raw: str, schema_hint: str) -> str:
        prompt = _REPAIR_PROMPT.format(schema_hint=schema_hint, raw=raw)
        config: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": 0.0,
        }
        if self._api_base:
            config["api_base"] = self._api_base

        async with self._semaphore:
            try:
                return await self._llm_port.query(prompt, config)
            except LLMError as exc:
                logger.warning(
                    "Format repairer LLM call failed (model=%s): %s; "
                    "falling back to original error",
                    self._model,
                    exc,
                )
                return raw
