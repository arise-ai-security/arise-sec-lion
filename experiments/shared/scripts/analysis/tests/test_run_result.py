"""Unit tests for ``compute_run_result`` (design doc §13.2 + §13.6).

Mocks ``fetch_run_events`` so the composer can be exercised without a
live database. Patches the binding inside ``run_result`` — not the source
module — because the composer captured the function at import time.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from experiments.shared.scripts.analysis.errors import (
    NotABossRunError,
    UnknownFamilyError,
    UnknownRunError,
)
from experiments.shared.scripts.analysis.models import (
    CostMetrics,
    HierarchyMetrics,
    LimitMetrics,
    OutcomeMetrics,
    QuantitativeMetrics,
    RunResult,
    TimingMetrics,
    ToolMetrics,
)
from experiments.shared.scripts.analysis.run_result import compute_run_result
from experiments.shared.scripts.analysis.tests.factories import (
    agent_created,
    run_completed,
    run_started,
    tool_use_event,
)


_FETCH_TARGET = (
    "experiments.shared.scripts.analysis.run_result.fetch_run_events"
)


pytestmark = pytest.mark.asyncio


async def test_happy_path_returns_fully_populated_run_result():
    # Given: a synthetic boss-rooted event stream (RunStarted +
    # AgentCreated + tool_use + RunCompleted) on a single aggregate.
    run_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        agent_created(run_id, 2, role="boss", offset_ms=10),
        tool_use_event(run_id, 3, "Read", {"file_path": "README.md"}, offset_ms=20),
        tool_use_event(run_id, 4, "Bash", {"command": "ls -la"}, offset_ms=30),
        run_completed(run_id, 5, duration_seconds=1.0, offset_ms=1_000),
    ]
    conn = MagicMock()

    # When: compute_run_result runs against a mocked fetch_run_events.
    with patch(_FETCH_TARGET, new=AsyncMock(return_value=events)):
        result = await compute_run_result(conn, run_id, family="A")

    # Then: RunResult is well-formed and each sub-metric has the right type.
    assert isinstance(result, RunResult)
    assert result.run_id == run_id
    assert result.qualitative is None
    qm = result.quantitative
    assert isinstance(qm, QuantitativeMetrics)
    assert qm.run_id == run_id
    assert qm.family == "A"
    assert qm.event_count == len(events)
    assert qm.aggregate_count == 1
    assert isinstance(qm.cost, CostMetrics)
    assert isinstance(qm.tools, ToolMetrics)
    assert isinstance(qm.outcomes, OutcomeMetrics)
    assert isinstance(qm.hierarchy, HierarchyMetrics)
    assert isinstance(qm.timing, TimingMetrics)
    assert isinstance(qm.limits, LimitMetrics)


async def test_happy_path_aggregate_count_uses_distinct_aggregates():
    # Given: a boss with one child aggregate emitting an extra event.
    run_id = uuid4()
    child_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        agent_created(child_id, 1, role="worker", parent_id=run_id, offset_ms=10),
        run_completed(run_id, 2, offset_ms=1_000),
    ]
    conn = MagicMock()

    # When: compute_run_result runs.
    with patch(_FETCH_TARGET, new=AsyncMock(return_value=events)):
        result = await compute_run_result(conn, run_id)

    # Then: aggregate_count reflects the two distinct aggregate ids.
    assert result.quantitative.aggregate_count == 2
    assert result.quantitative.event_count == 3


async def test_empty_event_stream_raises_unknown_run_error():
    # Given: a fetch that returns no events for run_id.
    run_id = uuid4()
    conn = MagicMock()

    # When/Then: compute_run_result raises UnknownRunError carrying run_id.
    with (
        patch(_FETCH_TARGET, new=AsyncMock(return_value=[])),
        pytest.raises(UnknownRunError) as exc_info,
    ):
        await compute_run_result(conn, run_id)
    assert exc_info.value.run_id == run_id


async def test_non_boss_first_event_type_raises_not_a_boss_run_error():
    # Given: the first event is AgentCreated on run_id (not RunStarted).
    run_id = uuid4()
    events = [agent_created(run_id, 1, role="worker", offset_ms=0)]
    conn = MagicMock()

    # When/Then: compute_run_result rejects the non-boss shape.
    with (
        patch(_FETCH_TARGET, new=AsyncMock(return_value=events)),
        pytest.raises(NotABossRunError) as exc_info,
    ):
        await compute_run_result(conn, run_id)
    err = exc_info.value
    assert err.run_id == run_id
    assert err.first_event_type == "AgentCreated"
    assert err.first_aggregate_id == run_id


async def test_run_started_on_different_aggregate_raises_not_a_boss_run_error():
    # Given: RunStarted exists but on a DIFFERENT aggregate id.
    run_id = uuid4()
    other_id = uuid4()
    events = [run_started(other_id, 1, offset_ms=0)]
    conn = MagicMock()

    # When/Then: aggregate-id mismatch is rejected with diagnostic fields.
    with (
        patch(_FETCH_TARGET, new=AsyncMock(return_value=events)),
        pytest.raises(NotABossRunError) as exc_info,
    ):
        await compute_run_result(conn, run_id)
    err = exc_info.value
    assert err.run_id == run_id
    assert err.first_event_type == "RunStarted"
    assert err.first_aggregate_id == other_id


async def test_unsupported_family_bubbles_unknown_family_error_from_compute_tools():
    # Given: a valid boss-rooted stream but an unsupported family.
    run_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        run_completed(run_id, 2, offset_ms=1_000),
    ]
    conn = MagicMock()

    # When/Then: compute_tools raises UnknownFamilyError; composer propagates it.
    with (
        patch(_FETCH_TARGET, new=AsyncMock(return_value=events)),
        pytest.raises(UnknownFamilyError) as exc_info,
    ):
        await compute_run_result(conn, run_id, family="B")
    assert exc_info.value.family == "B"


async def test_fetch_run_events_called_with_supplied_conn_and_run_id():
    # Given: a boss-rooted event stream and a mock conn object.
    run_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        run_completed(run_id, 2, offset_ms=1_000),
    ]
    conn = MagicMock()
    fake_fetch = AsyncMock(return_value=events)

    # When: compute_run_result runs.
    with patch(_FETCH_TARGET, new=fake_fetch):
        await compute_run_result(conn, run_id)

    # Then: the patched fetcher was invoked exactly once with (conn, run_id).
    fake_fetch.assert_awaited_once_with(conn, run_id)
