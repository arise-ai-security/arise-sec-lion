"""Tests for `infrastructure.io.robust_call`."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from infrastructure.io.robust_call import RetryPolicy, run


class _RetryableError(Exception):
    """Marker exception treated as retryable by the policy under test."""


class _FatalError(Exception):
    """Marker exception NOT in the retryable list -- should bubble immediately."""


async def test_succeeds_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory returns OK on first call; no retries, no sleeps."""

    # Given: factory that always succeeds, sleep patched to detect any usage
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return "ok"

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    policy = RetryPolicy(max_retries=3, base_delay=0.1, retryable=(_RetryableError,))

    # When: run is invoked
    result = await run(factory, timeout=1.0, policy=policy)

    # Then: result is the factory's value, factory called exactly once, no sleep
    assert result == "ok"
    assert calls == 1
    assert sleep_mock.call_count == 0


async def test_retries_on_retryable_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory raises once then succeeds; two attempts, one sleep."""

    # Given: factory that fails once then succeeds
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _RetryableError("transient")
        return 42

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    policy = RetryPolicy(max_retries=3, base_delay=0.1, retryable=(_RetryableError,))

    # When: run is invoked
    result = await run(factory, timeout=1.0, policy=policy)

    # Then: factory called twice, slept once, result correct
    assert result == 42
    assert calls == 2
    assert sleep_mock.call_count == 1


async def test_exhausts_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory always raises retryable; raises after max_retries+1 calls."""

    # Given: factory that always raises a retryable exception
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        raise _RetryableError(f"call_{calls}")

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    policy = RetryPolicy(max_retries=2, base_delay=0.1, retryable=(_RetryableError,))

    # When/Then: run raises the retryable exception unwrapped
    with pytest.raises(_RetryableError) as exc_info:
        await run(factory, timeout=1.0, policy=policy)

    # And: factory called max_retries + 1 times, sleep called max_retries times
    assert calls == 3  # max_retries=2 -> 3 total attempts
    assert sleep_mock.call_count == 2
    assert "call_3" in str(exc_info.value)


async def test_non_retryable_propagates_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-retryable exception bubbles on first attempt with no sleep."""

    # Given: factory that raises a non-retryable exception
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        raise _FatalError("nope")

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    policy = RetryPolicy(max_retries=3, base_delay=0.1, retryable=(_RetryableError,))

    # When/Then: fatal exception is re-raised on first attempt
    with pytest.raises(_FatalError):
        await run(factory, timeout=1.0, policy=policy)

    # And: factory called exactly once, no sleep
    assert calls == 1
    assert sleep_mock.call_count == 0


async def test_jitter_window_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each retry sleep is within [base * backoff_base**i * 0.5, * 1.5]."""

    # Given: factory that always raises retryable; sleeps captured
    async def factory():
        raise _RetryableError("always")

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    base = 0.1
    backoff = 2.0
    max_retries = 3
    policy = RetryPolicy(
        max_retries=max_retries,
        base_delay=base,
        jitter_range=(0.5, 1.5),
        backoff_base=backoff,
        retryable=(_RetryableError,),
    )

    # When: run exhausts retries
    with pytest.raises(_RetryableError):
        await run(factory, timeout=1.0, policy=policy)

    # Then: exactly max_retries sleeps recorded, each within jitter window
    assert sleep_mock.call_count == max_retries
    for i, call in enumerate(sleep_mock.call_args_list):
        delay = call.args[0]
        planned = base * (backoff**i)
        assert planned * 0.5 <= delay <= planned * 1.5, (
            f"attempt {i}: delay {delay} not in [{planned * 0.5}, {planned * 1.5}]"
        )


async def test_timeout_propagates() -> None:
    """A factory that hangs longer than `timeout` raises asyncio.TimeoutError."""

    # Given: factory whose awaitable sleeps far longer than timeout
    async def factory():
        await asyncio.sleep(10.0)
        return "never"

    # Empty retryable: timeout should propagate, not retry
    policy = RetryPolicy(max_retries=2, base_delay=0.01, retryable=())

    # When/Then: TimeoutError raised within a small wall budget
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run(factory, timeout=0.05, policy=policy),
            timeout=1.0,
        )


async def test_on_retry_callback_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """`on_retry` is invoked once per retry with (attempt, exc, delay)."""

    # Given: factory that fails twice then succeeds
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _RetryableError(f"fail_{calls}")
        return "done"

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    seen: list[tuple[int, BaseException, float]] = []

    def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        seen.append((attempt, exc, delay))

    policy = RetryPolicy(max_retries=3, base_delay=0.1, retryable=(_RetryableError,))

    # When: run completes after retries
    result = await run(factory, timeout=1.0, policy=policy, on_retry=on_retry)

    # Then: callback invoked twice with attempt indices 0 and 1
    assert result == "done"
    assert len(seen) == 2
    assert seen[0][0] == 0
    assert seen[1][0] == 1
    assert isinstance(seen[0][1], _RetryableError)
    assert isinstance(seen[1][1], _RetryableError)
    # And: delays passed to callback match what asyncio.sleep received
    assert seen[0][2] == sleep_mock.call_args_list[0].args[0]
    assert seen[1][2] == sleep_mock.call_args_list[1].args[0]
