"""LLM-backed judge for :func:`evaluate_run` (boundary-clean; litellm only).

The evaluation package must NOT import ``plugins/security`` (composition-root
boundary). This judge reaches the model through litellm — the same generic
provider gateway the runtime's :class:`LiteLLMAdapter` uses — so it stays
boundary-clean. It is exactly the callable ``evaluate_run(..., judge=...)``
expects: given one built prompt dict ``{prompt, excerpts, schema}`` it returns the
validated verdict dict.

Structured output: a builder ``schema`` is a *compact* ``{"verdict": "bool", ...}``
contract, NOT a JSON Schema. :func:`_json_schema_from_contract` translates it into
an OpenAI structured-outputs schema (every key ``required``,
``additionalProperties: false``, ``strict: true``) so the model must return
exactly the verdict shape.

Failure policy: a judge call that errors, times out, or returns unparseable output
FAILS CLOSED (``verdict=False`` + ``_error``) so a strict gate never silently
passes on a broken judge. litellm 1.83 has no cost-map entry for gpt-5.5, so cost
is computed from ``response.usage`` here rather than via ``litellm.get_model_info``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import litellm


logger = logging.getLogger(__name__)

# Pinned dated snapshot for reproducibility across the experiment window (the
# floating ``gpt-5.5`` alias would drift). gpt-5.5 is OpenAI's flagship as of
# 2026-04; reasoning models ignore ``temperature`` so it is never sent.
DEFAULT_JUDGE_MODEL = "gpt-5.5-2026-04-23"
DEFAULT_REASONING_EFFORT = "high"
_MAX_COMPLETION_TOKENS = 8000
_NUM_RETRIES = 2

_SCALAR_JSON_TYPE: dict[str, str] = {
    "bool": "boolean",
    "float": "number",
    "int": "integer",
    "str": "string",
}


def _scalar_schema(type_token: str) -> dict[str, Any]:
    return {"type": _SCALAR_JSON_TYPE.get(type_token.strip().lower(), "string")}


def _field_schema(type_spec: str) -> dict[str, Any]:
    """Map one compact type token to a JSON Schema fragment (scalars + ``list[T]``)."""
    spec = type_spec.strip().lower()
    if spec.startswith("list[") and spec.endswith("]"):
        return {"type": "array", "items": _scalar_schema(spec[len("list[") : -1])}
    if spec in ("list", "array"):
        return {"type": "array", "items": {"type": "string"}}
    return _scalar_schema(spec)


def _json_schema_from_contract(contract: dict[str, str]) -> dict[str, Any]:
    """Translate the builder's ``{key: type}`` contract into a strict OpenAI schema.

    OpenAI structured outputs require every property in ``required`` and
    ``additionalProperties: false``; omitting either is a 400.
    """
    return {
        "type": "object",
        "properties": {key: _field_schema(t) for key, t in contract.items()},
        "required": list(contract),
        "additionalProperties": False,
    }


def _coerce(value: Any, type_spec: str) -> Any:
    """Defensively coerce a returned field to its contract type."""
    spec = type_spec.strip().lower()
    try:
        if spec == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "1", "yes")
        if spec == "float":
            return float(value)
        if spec == "int":
            return int(value)
        if spec.startswith("list"):
            return list(value) if isinstance(value, (list, tuple)) else [value]
        return str(value)
    except (TypeError, ValueError):
        return value


def _fail_closed(contract: dict[str, str], reason: str) -> dict[str, Any]:
    """A safe verdict for a failed judge call: ``verdict=False`` + ``_error``."""
    out: dict[str, Any] = {}
    for key, type_spec in contract.items():
        spec = type_spec.strip().lower()
        if spec.startswith("list"):
            out[key] = []
        elif spec == "float":
            out[key] = 0.0
        elif spec == "int":
            out[key] = 0
        else:
            out[key] = "" if spec == "str" else False
    out["verdict"] = False
    if "reason" in contract:
        out["reason"] = reason
    out["_error"] = True
    return out


class LLMJudge:
    """Callable judge for ``evaluate_run(judge=...)``: one built prompt -> verdict dict.

    Tracks cumulative call count + token usage so the harness can report judge
    cost separately from the experiment runs' cost.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_JUDGE_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_completion_tokens: int = _MAX_COMPLETION_TOKENS,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self.calls = 0
        self.errors = 0
        self.cost_usd = 0.0
        self.usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
        }

    def __call__(self, built: dict[str, Any]) -> dict[str, Any]:
        contract: dict[str, str] = built["schema"]
        self.calls += 1
        # Optional params go through a dict so litellm's narrow Literal typing for
        # reasoning_effort does not reject our str config; drop_params lets litellm
        # silently drop any param a model does not support.
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": built["prompt"]}],
            "max_completion_tokens": self.max_completion_tokens,
            "reasoning_effort": self.reasoning_effort,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "verdict",
                    "schema": _json_schema_from_contract(contract),
                    "strict": True,
                },
            },
            "num_retries": _NUM_RETRIES,
            "drop_params": True,
        }
        try:
            response: Any = litellm.completion(**request)
        except Exception as exc:  # noqa: BLE001 — litellm raises many provider types; fail closed
            self.errors += 1
            logger.warning("Judge LLM call failed (%s): %s", self.model, exc)
            return _fail_closed(contract, f"judge_error: {type(exc).__name__}: {exc}")

        self._record_usage(response)
        content = response.choices[0].message.content
        if not content:
            self.errors += 1
            logger.warning("Judge returned empty content (%s)", self.model)
            return _fail_closed(contract, "judge_error: empty content")
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            self.errors += 1
            logger.warning("Judge returned non-JSON output: %s", exc)
            return _fail_closed(contract, f"judge_error: invalid json: {exc}")
        if not isinstance(data, dict):
            self.errors += 1
            return _fail_closed(contract, "judge_error: response is not a JSON object")
        return {key: _coerce(data.get(key), type_spec) for key, type_spec in contract.items()}

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            self.usage["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                self.usage["cache_read_tokens"] += getattr(details, "cached_tokens", 0) or 0
        try:
            self.cost_usd += float(litellm.completion_cost(completion_response=response) or 0.0)
        except Exception:  # noqa: BLE001 — no cost-map entry for gpt-5.5 in litellm 1.83
            pass
