"""Tests for the LLM judge callable: schema translation, validation, fail-closed.

The live LLM call is monkeypatched — these stay offline and deterministic. The
schema-translation test is the most important: OpenAI structured outputs reject a
schema that is missing ``additionalProperties: false`` or omits any key from
``required``, so a regression here would 400 every judge call in production.
"""

from __future__ import annotations

import experiments.shared.evaluation.judge as judge_mod
from experiments.shared.evaluation.judge import (
    LLMJudge,
    _fail_closed,
    _json_schema_from_contract,
    _matches_type,
)


_CONTRACT = {
    "verdict": "bool",
    "confidence": "float",
    "reason": "str",
    "evidence_refs": "list[str]",
}


def test_json_schema_translation_is_strict_structured_output() -> None:
    """The compact contract becomes a strict OpenAI schema (all keys required, no extras)."""
    schema = _json_schema_from_contract(_CONTRACT)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(_CONTRACT)  # OpenAI strict: every key required
    assert schema["properties"]["verdict"] == {"type": "boolean"}
    assert schema["properties"]["confidence"] == {"type": "number"}
    assert schema["properties"]["evidence_refs"] == {"type": "array", "items": {"type": "string"}}


def test_contract_type_validation_is_strict() -> None:
    assert _matches_type(True, "bool")
    assert not _matches_type("true", "bool")
    assert _matches_type(0.5, "float")
    assert not _matches_type("0.5", "float")
    assert _matches_type(3, "int")
    assert _matches_type("x", "str")
    assert not _matches_type(3, "str")
    assert _matches_type(["a", "b"], "list[str]")
    assert not _matches_type("solo", "list[str]")


def test_fail_closed_returns_false_verdict_with_marker() -> None:
    fc = _fail_closed(_CONTRACT, "boom")
    assert fc["verdict"] is False
    assert fc["_error"] is True
    assert fc["reason"] == "boom"
    assert fc["evidence_refs"] == []
    assert fc["confidence"] == 0.0


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    prompt_tokens_details = None


class _FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


def test_judge_parses_structured_response(monkeypatch) -> None:
    """A well-formed structured response is validated against the contract."""

    def fake_completion(**kwargs):
        # Regression guard: the request must use strict json_schema structured output.
        rf = kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["schema"]["additionalProperties"] is False
        return _FakeResponse(
            '{"verdict": true, "confidence": 0.9, "reason": "ok", "evidence_refs": ["a"]}'
        )

    monkeypatch.setattr(judge_mod.litellm, "completion", fake_completion)
    judge = LLMJudge()
    out = judge({"prompt": "p", "excerpts": {}, "schema": _CONTRACT})
    assert out["verdict"] is True
    assert out["confidence"] == 0.9
    assert out["evidence_refs"] == ["a"]
    assert judge.calls == 1
    assert judge.errors == 0


def test_judge_fails_closed_on_llm_error(monkeypatch) -> None:
    """A raised LLM error becomes a fail-closed verdict (never silently passes)."""

    def boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(judge_mod.litellm, "completion", boom)
    judge = LLMJudge()
    out = judge({"prompt": "p", "excerpts": {}, "schema": _CONTRACT})
    assert out["verdict"] is False
    assert out["_error"] is True
    assert judge.errors == 1


def test_judge_fails_closed_on_bad_json(monkeypatch) -> None:
    """Non-JSON content fails closed rather than raising into the evaluator."""
    monkeypatch.setattr(judge_mod.litellm, "completion", lambda **k: _FakeResponse("not json"))
    judge = LLMJudge()
    out = judge({"prompt": "p", "excerpts": {}, "schema": _CONTRACT})
    assert out["verdict"] is False
    assert out["_error"] is True


def test_judge_fails_closed_on_empty_content(monkeypatch) -> None:
    monkeypatch.setattr(judge_mod.litellm, "completion", lambda **k: _FakeResponse(None))
    judge = LLMJudge()
    out = judge({"prompt": "p", "excerpts": {}, "schema": _CONTRACT})
    assert out["verdict"] is False
    assert out["_error"] is True


def test_judge_fails_closed_on_wrong_structured_types(monkeypatch) -> None:
    monkeypatch.setattr(
        judge_mod.litellm,
        "completion",
        lambda **k: _FakeResponse(
            '{"verdict": "true", "confidence": 0.9, '
            '"reason": "ok", "evidence_refs": ["a"]}'
        ),
    )
    judge = LLMJudge()

    out = judge({"prompt": "p", "excerpts": {}, "schema": _CONTRACT})

    assert out["verdict"] is False
    assert out["_error"] is True
    assert judge.errors == 1
