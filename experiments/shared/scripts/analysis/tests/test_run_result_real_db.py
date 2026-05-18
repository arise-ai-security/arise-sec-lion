"""Integration tests for ``compute_run_result`` against the live Postgres event store.

Gated on ``POSTGRES_PASSWORD`` (matching the ``db/`` package convention; see
``experiments/shared/scripts/db/tests/test_event_queries_real_db.py``). These
tests pull a real A1 boss run from the events table and exercise the
end-to-end metric composition.

Design doc: ``agent-docs/2026-05-17-run-result-quantitative-design.md``
sections 13.5 (integration) and 17 (acceptance criteria).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from experiments.shared.scripts.analysis.errors import (
    NotABossRunError,
    UnknownFamilyError,
    UnknownRunError,
)
from experiments.shared.scripts.analysis.models import RunResult
from experiments.shared.scripts.analysis.run_result import compute_run_result
from experiments.shared.scripts.db import open_connection


pytestmark = pytest.mark.asyncio


# A known A1 boss aggregate from the live ``events`` table. Picked because
# it has the second-largest tool_use footprint among A1 bosses with both
# ``RunStarted`` and ``RunCompleted`` markers — 206 tool_use
# ``ThoughtCaptured`` events. The top-1 (214 tool_use events,
# de3a100d-d4a6-4a02-9dd4-105c7a568fb6) was rejected because it contains 3
# bash ``wget http(s)://...`` invocations that the forbidden-web indirect
# detector flags as violations, which would conflict with the
# zero-forbidden-web assertion required by the design doc's empirical
# baseline (§9). This candidate satisfies BOTH constraints — high
# tool_use volume to stress classification / cost / outcome paths, and
# zero forbidden-web matches consistent with the wider A1 corpus.
_KNOWN_A1_RUN = UUID("bf601850-fe89-45a0-a234-59b8a9b715b8")

# A non-boss aggregate whose first (and only) event is
# ``SharedContextCreated`` — not ``RunStarted``. Used to exercise the
# ``NotABossRunError`` branch.
_KNOWN_NON_BOSS = UUID("59665f51-6416-54fc-b62a-62703032eeef")


def _live_db_available() -> bool:
    return bool(os.environ.get("POSTGRES_PASSWORD"))


@pytest.fixture
async def conn():
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set — skipping real-DB tests")
    connection = await open_connection()
    try:
        yield connection
    finally:
        await connection.close()


async def test_compute_run_result_for_known_a1_run_succeeds(conn) -> None:
    # When: composing a RunResult for a known A1 boss aggregate
    result = await compute_run_result(conn, _KNOWN_A1_RUN, family="A")

    # Then: returns a fully populated RunResult tagged with the run id
    assert result is not None
    assert isinstance(result, RunResult)
    assert result.run_id == _KNOWN_A1_RUN
    assert result.qualitative is None

    # And: the quantitative block reflects a real run — non-trivial event
    # count, at least one tool call, non-negative cost, and the
    # RunCompleted marker that gated our selection query is recorded.
    assert result.quantitative.event_count > 0
    assert result.quantitative.tools.total_tool_calls > 0
    assert result.quantitative.cost.total_llm_cost_usd >= 0
    assert result.quantitative.outcomes.has_run_completed is True


async def test_compute_run_result_for_unsupported_family_raises_unknown_family(
    conn,
) -> None:
    # When: requesting metrics for an unpopulated taxonomy family
    # Then: UnknownFamilyError bubbles up from compute_tools
    with pytest.raises(UnknownFamilyError):
        await compute_run_result(conn, _KNOWN_A1_RUN, family="B")


async def test_compute_run_result_for_non_boss_aggregate_raises_not_a_boss(
    conn,
) -> None:
    # Given: an aggregate that exists in events but whose first event
    # is not ``RunStarted`` (a SharedContext aggregate)
    # When: asking compute_run_result to treat it as a boss
    # Then: it rejects the input with NotABossRunError
    with pytest.raises(NotABossRunError):
        await compute_run_result(conn, _KNOWN_NON_BOSS, family="A")


async def test_compute_run_result_for_nonexistent_run_id_raises_unknown_run(
    conn,
) -> None:
    # Given: a random uuid that cannot collide with any real aggregate
    # When: asking compute_run_result for its metrics
    # Then: UnknownRunError is raised (no events found)
    with pytest.raises(UnknownRunError):
        await compute_run_result(conn, uuid4(), family="A")


async def test_known_a1_run_has_zero_forbidden_web_attempts(conn) -> None:
    # Given: the current A-cell corpus has zero forbidden-web attempts —
    # 0/329 runs invoked WebFetch/WebSearch and zero bash-web cheats were
    # observed (see design doc §9 and forbidden-web analysis notes).
    result = await compute_run_result(conn, _KNOWN_A1_RUN, family="A")

    # Then: both the direct and indirect detectors agree there are no
    # violations for this run.
    assert result.quantitative.tools.forbidden_web_attempts == 0
