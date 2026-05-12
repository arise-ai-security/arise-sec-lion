"""External-call resilience wrapper with per-attempt timeout and retry policy."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar


logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Policy describing retry behavior for a `run` call.

    Attributes:
        max_retries: Number of additional attempts after the first one. Total
            attempts == ``max_retries + 1``.
        base_delay: Base delay (seconds) before the first retry sleep.
        jitter_range: Multiplicative jitter range applied to each sleep, sampled
            uniformly. ``(0.5, 1.5)`` means each delay is drawn in
            ``[0.5 * planned, 1.5 * planned]``.
        backoff_base: Exponential backoff base. Sleep before retry ``attempt``
            (zero-indexed) is ``base_delay * backoff_base ** attempt * jitter``.
        retryable: Tuple of exception types eligible for retry. Anything else
            propagates immediately. An empty tuple means nothing is retryable.
    """

    max_retries: int
    base_delay: float
    jitter_range: tuple[float, float] = (0.5, 1.5)
    backoff_base: float = 2.0
    retryable: tuple[type[BaseException], ...] = ()


async def run(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    timeout: float,
    policy: RetryPolicy,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run a zero-arg awaitable factory with per-attempt timeout and retries.

    ``coro_factory`` must be a zero-argument callable that returns a fresh
    awaitable each call -- passing a coroutine directly would cause retries
    to replay an already-awaited coroutine and raise ``RuntimeError``.

    Args:
        coro_factory: Zero-arg callable returning a fresh awaitable per attempt.
        timeout: Per-attempt timeout (seconds), applied via ``asyncio.wait_for``.
        policy: Retry behavior. See ``RetryPolicy``.
        on_retry: Optional callback invoked just before each retry sleep,
            receiving ``(attempt, exc, sleep_seconds)``. ``attempt`` is the
            zero-indexed attempt that just failed.

    Returns:
        Whatever the awaitable resolves to.

    Raises:
        Any exception from ``coro_factory`` that is not in ``policy.retryable``
        propagates immediately. The final retryable exception (after
        ``max_retries + 1`` attempts) also propagates as itself. ``asyncio.wait_for``
        raises ``asyncio.TimeoutError`` on per-attempt timeout, which is treated
        like any other exception -- retried only if it appears in
        ``policy.retryable``.
    """
    for attempt in range(policy.max_retries + 1):
        try:
            return await asyncio.wait_for(coro_factory(), timeout=timeout)
        except policy.retryable as exc:
            if attempt >= policy.max_retries:
                raise
            delay = (
                policy.base_delay
                * (policy.backoff_base**attempt)
                * random.uniform(*policy.jitter_range)
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)

    # Unreachable: loop either returns or raises. Present for type checker.
    raise RuntimeError("robust_call.run exited loop without return or raise")
