"""Tests for the env-gated manager-cache prime (ARISE_PRIME_MANAGER_CACHE).

OpenAI serves a cached entry only when it is a complete prefix of the new request, so
the prime must send the managers' *longest common prefix*. ``_longest_common_prefix`` is
the load-bearing piece: its result is, by definition, a true byte-prefix of every manager
prompt, which is exactly what the cache needs. The end-to-end cache effect is validated by
a real B4 run, not here.
"""

import pytest

from bootstrap.tests.test_characterization import FakeLLM, FakeWorkerTool, InMemoryEventStore
from core.application.agent_orchestrator import _longest_common_prefix
from core.application.tests.test_system_loop_timeout import _wire_service


def test_longest_common_prefix_basic() -> None:
    assert _longest_common_prefix(["abcXYZ", "abcDEF", "abc123"]) == "abc"


def test_longest_common_prefix_is_a_true_prefix_of_every_input() -> None:
    items = ["shared-head-AAAA-tail1", "shared-head-AAAA-tail2", "shared-head-BBBB"]
    lcp = _longest_common_prefix(items)
    assert lcp == "shared-head-"
    assert all(s.startswith(lcp) for s in items)


def test_longest_common_prefix_edge_cases() -> None:
    assert _longest_common_prefix([]) == ""
    assert _longest_common_prefix(["solo"]) == "solo"
    assert _longest_common_prefix(["abc", "xyz"]) == ""  # no shared head
    assert _longest_common_prefix(["abc", "abc"]) == "abc"  # identical
    assert _longest_common_prefix(["ab", "abcd"]) == "ab"  # one is a prefix of the other


@pytest.mark.asyncio
async def test_prime_skips_below_two_managers() -> None:
    """With <2 agents there is no shared prefix to warm, so prime is a no-op (returns 0)
    and issues no LLM call (FakeLLM has no query_with_tools — a call would raise)."""
    service = _wire_service(InMemoryEventStore(), FakeLLM(), FakeWorkerTool())
    orchestrator = service._orchestrator

    assert await orchestrator.prime_manager_cache([]) == 0

    boss_id = await service.create_boss_agent("Prime guard test")
    boss = await service._load_agent_with_context(boss_id)
    assert await orchestrator.prime_manager_cache([boss]) == 0
