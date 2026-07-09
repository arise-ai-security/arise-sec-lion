"""Integration tests against the live Postgres event store.

Skipped unless POSTGRES_PASSWORD (and friends) are set in the environment
and a known boss run id is reachable. Validates that:

  1. The recursive subtree CTE returns at least the boss itself.
  2. `fetch_run_events(run_id)` agrees with `fetch_agent_events(run_id)`
     for flat-mode runs (where the subtree IS the boss).
  3. Every row that comes back has `payload` decoded into a `dict` —
     i.e. the JSONB codec is registered correctly.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest

from experiments.shared.scripts.db import (
    fetch_agent_events,
    fetch_run_events,
    get_agent_trajectory,
    get_parent_trace,
    open_connection,
)


pytestmark = pytest.mark.asyncio


# A known flat-mode A1 run from `a12-batch-autogen` (see
# `experiments/a12-batch-autogen/reports/db_run_validity.csv`).
# Picking a small one keeps the test fast.
_KNOWN_A1_RUN = UUID("73ee8ded-554a-430b-b1a3-719d85653690")


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


class TestRealDb:
    async def test_known_a1_run_has_events(self, conn) -> None:
        # When: pulling the subtree for a known good A1 run
        events = await fetch_run_events(conn, _KNOWN_A1_RUN)

        # Then: we get more than zero events and the boss agent is
        # represented (its AgentCreated is at sequence_number=1).
        assert len(events) > 0
        boss_created = [
            e for e in events
            if e.event_type == "AgentCreated" and e.aggregate_id == _KNOWN_A1_RUN
        ]
        assert len(boss_created) == 1
        assert boss_created[0].sequence_number == 1
        assert boss_created[0].payload["role"] == "boss"

    async def test_subtree_equals_self_for_flat_mode(self, conn) -> None:
        # Given: A-cell runs are flat — boss is the only agent.
        # When: comparing subtree count to single-agent count
        subtree = await fetch_run_events(conn, _KNOWN_A1_RUN)
        agent_only = await fetch_agent_events(conn, _KNOWN_A1_RUN)

        # Then: the two are identical (same event_ids, same order)
        assert len(subtree) == len(agent_only)
        assert [e.event_id for e in subtree] == [e.event_id for e in agent_only]

    async def test_jsonb_decoded_to_dict(self, conn) -> None:
        # Given: any event from the known run
        events = await fetch_run_events(conn, _KNOWN_A1_RUN)
        assert events  # sanity

        # Then: every payload is a `dict`, not a JSON-string (which means
        # the codec is registered correctly on the connection)
        assert all(isinstance(e.payload, dict) for e in events)

    async def test_get_agent_trajectory_pretty_lines(self, conn) -> None:
        # When: requesting the trajectory in pretty form
        lines = await get_agent_trajectory(conn, _KNOWN_A1_RUN, pretty=True)

        # Then: returns a non-empty list of strings; the first line is
        # the header summary and subsequent lines have a "[seq=" prefix.
        assert isinstance(lines, list)
        assert len(lines) >= 2
        assert lines[0].startswith("trajectory")
        assert any("[seq=" in ln for ln in lines[1:])

    async def test_lineage_of_boss_equals_subtree_for_flat_mode(self, conn) -> None:
        # Given: A-cell runs are flat — boss is both root and leaf,
        # so walking UP from boss should yield the same set as walking
        # DOWN from boss (both stop at the same single aggregate).
        subtree = await fetch_run_events(conn, _KNOWN_A1_RUN)
        lineage = await get_parent_trace(conn, _KNOWN_A1_RUN)

        # Then: identical event_id sets (order may differ slightly
        # because subtree sorts by (occurred_at, sequence_number) at
        # the SQL layer, while lineage re-sorts with aggregate_id as
        # tertiary key — for a single-aggregate set the two are equal).
        assert isinstance(lineage, list)
        assert {e.event_id for e in subtree} == {e.event_id for e in lineage}
