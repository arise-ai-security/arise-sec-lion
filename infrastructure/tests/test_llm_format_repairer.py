"""Tests for the LLM-backed output-format repairer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.domain.exceptions import LLMError
from infrastructure.adapters.llm_format_repairer import LLMFormatRepairer


class _FakeLLM:
    """Minimal LLMPort implementation for tests."""

    def __init__(self, response: str | Exception = "{}") -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        self.calls.append((prompt, config_dict))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def query_with_usage(self, *args, **kwargs):  # noqa: ANN
        raise NotImplementedError

    async def query_with_tools(self, *args, **kwargs):  # noqa: ANN
        raise NotImplementedError


@pytest.mark.asyncio
async def test_repair_returns_llm_output_when_call_succeeds() -> None:
    # Given: a fake LLM that returns a clean repaired payload
    fake = _FakeLLM(response='{"action": "execute"}')
    repairer = LLMFormatRepairer(llm_port=fake, model="ollama_chat/qwen3-coder:480b-cloud")

    # When: repair() is invoked on malformed input
    out = await repairer.repair('{"action": "execute"', schema_hint="<hint>")

    # Then: the repaired text is returned and the model was called once
    assert out == '{"action": "execute"}'
    assert len(fake.calls) == 1
    prompt, config = fake.calls[0]
    assert "<hint>" in prompt
    assert '{"action": "execute"' in prompt
    assert config["model"] == "ollama_chat/qwen3-coder:480b-cloud"
    assert config["temperature"] == 0.0


@pytest.mark.asyncio
async def test_repair_returns_raw_unchanged_on_llm_error() -> None:
    # Given: an LLM that raises LLMError (e.g. service unavailable)
    fake = _FakeLLM(response=LLMError("ollama unreachable"))
    repairer = LLMFormatRepairer(llm_port=fake, model="ollama_chat/qwen3-coder:480b-cloud")

    # When: repair() is invoked
    raw = "garbage }{"
    out = await repairer.repair(raw, schema_hint="<hint>")

    # Then: input is returned unchanged so the caller can surface its
    # original parse error rather than masking it with a fixer error.
    assert out == raw


@pytest.mark.asyncio
async def test_repair_passes_api_base_when_set() -> None:
    fake = _FakeLLM(response="{}")
    repairer = LLMFormatRepairer(
        llm_port=fake,
        model="ollama_chat/qwen3-coder:480b-cloud",
        api_base="https://ollama.com",
    )
    await repairer.repair("x", schema_hint="y")
    _, config = fake.calls[0]
    assert config["api_base"] == "https://ollama.com"


@pytest.mark.asyncio
async def test_repair_omits_api_base_when_unset() -> None:
    fake = _FakeLLM(response="{}")
    repairer = LLMFormatRepairer(llm_port=fake, model="ollama_chat/qwen3-coder:480b-cloud")
    await repairer.repair("x", schema_hint="y")
    _, config = fake.calls[0]
    assert "api_base" not in config


class _CountingLLM:
    """LLMPort stub that records concurrent in-flight calls.

    Each `query` sleeps for `latency` seconds, incrementing an in-flight
    counter on entry and decrementing on exit. Tracks the running peak.
    """

    def __init__(self, latency: float = 0.05) -> None:
        self._latency = latency
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = asyncio.Lock()
        self.calls = 0

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        async with self._lock:
            self.in_flight += 1
            self.calls += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight
        try:
            await asyncio.sleep(self._latency)
            return "{}"
        finally:
            async with self._lock:
                self.in_flight -= 1

    async def query_with_usage(self, *args, **kwargs):  # noqa: ANN
        raise NotImplementedError

    async def query_with_tools(self, *args, **kwargs):  # noqa: ANN
        raise NotImplementedError


@pytest.mark.asyncio
async def test_bounded_by_semaphore() -> None:
    """Concurrent `repair` calls are capped by `max_concurrent`.

    Fires 6 concurrent `repair` calls against a repairer configured with
    `max_concurrent=2`. The stub LLM tracks how many calls overlap; the
    peak must never exceed 2.
    """

    # Given: a counting LLM and a repairer capped at 2 concurrent calls
    fake = _CountingLLM(latency=0.05)
    repairer = LLMFormatRepairer(
        llm_port=fake,
        model="ollama_chat/qwen3-coder:480b-cloud",
        max_concurrent=2,
    )

    # When: 6 concurrent repair calls are dispatched
    results = await asyncio.gather(
        *[repairer.repair(f"raw-{i}", schema_hint="<hint>") for i in range(6)]
    )

    # Then: every call completed and the LLM saw all six requests
    assert len(results) == 6
    assert fake.calls == 6

    # And: max concurrent in-flight never exceeded the configured cap
    assert fake.max_in_flight <= 2, (
        f"max_in_flight={fake.max_in_flight} exceeds cap of 2"
    )
    # And: parallelism was actually used (>1 in-flight at some point);
    # otherwise the test would not distinguish a semaphore from a serial
    # implementation. Six 50ms calls cannot complete serially under the
    # test's wall budget without overlapping at all.
    assert fake.max_in_flight >= 2, (
        f"expected real parallelism; observed peak {fake.max_in_flight}"
    )
