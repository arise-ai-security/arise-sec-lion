"""Tests for the SQL dump rendering (pure; the DB fetch needs no coverage here)."""

from datetime import UTC, datetime

from experiments.shared.evaluation.export import render_events_dump


def _row(**overrides) -> dict:
    row = {
        "event_id": "11111111-1111-1111-1111-111111111111",
        "aggregate_id": "22222222-2222-2222-2222-222222222222",
        "sequence_number": 0,
        "event_type": "AgentCreated",
        "payload": '{"role": "boss"}',
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "metadata": "{}",
    }
    row.update(overrides)
    return row


def test_render_dump_structure() -> None:
    """The dump has the SET pragma, a typed INSERT, and an idempotent ON CONFLICT."""
    # Given/When
    sql = render_events_dump([_row()], include_schema=False)
    # Then
    assert "SET standard_conforming_strings = on;" in sql
    assert (
        "INSERT INTO events "
        "(event_id, aggregate_id, sequence_number, event_type, payload, occurred_at, metadata)"
        in sql
    )
    assert "ON CONFLICT (aggregate_id, sequence_number) DO NOTHING;" in sql
    # And: typed casts and an ISO timestamp literal
    assert "'{\"role\": \"boss\"}'::jsonb" in sql
    assert "'2026-01-01T00:00:00+00:00'::timestamptz" in sql


def test_render_dump_escapes_single_quotes() -> None:
    """Single quotes in JSON payloads are doubled so the literal stays valid."""
    # Given: a payload containing a single quote
    sql = render_events_dump([_row(payload="{\"msg\": \"it's fine\"}")], include_schema=False)
    # Then
    assert "it''s fine" in sql


def test_render_dump_null_metadata() -> None:
    """A NULL column renders as an unquoted NULL cast, not an empty string."""
    # Given/When
    sql = render_events_dump([_row(metadata=None)], include_schema=False)
    # Then
    assert "NULL::jsonb)" in sql


def test_render_dump_batches_rows() -> None:
    """All rows are emitted (multiple value tuples in one INSERT)."""
    # Given: three rows with distinct sequence numbers
    rows = [_row(sequence_number=n) for n in range(3)]
    # When
    sql = render_events_dump(rows, include_schema=False)
    # Then
    assert sql.count("AgentCreated") == 3
    assert sql.count("INSERT INTO events") == 1  # one batched INSERT
