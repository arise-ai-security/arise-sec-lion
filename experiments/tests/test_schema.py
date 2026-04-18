"""Tests for the normalized event schema (experiments.schema)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from experiments.schema import (
    NormalizedEvent,
    RunMeta,
    TokensConsumedPayload,
    ToolUsePayload,
)


def test_normalized_event_round_trip_tool_use() -> None:
    """tool_use NormalizedEvent serializes and deserializes losslessly."""
    # Given: a tool_use event with structured input and call_id
    payload = ToolUsePayload(
        call_id="tu_abc",
        tool_name="Bash",
        tool_input={"command": "ls /src"},
        duration_ms=42,
    )
    event = NormalizedEvent(
        event_id=str(uuid4()),
        run_id="test.cve-0-A1-0",
        occurred_at=datetime.now(timezone.utc),
        event_type="tool_use",
        source="flat_cli",
        agent_id=None,
        parent_agent_id=None,
        role="FLAT",
        depth=0,
        sequence_number=1,
        payload=payload,
    )

    # When: serialized and reparsed
    dumped = event.model_dump_json()
    reparsed = NormalizedEvent.model_validate_json(dumped)

    # Then: round-trip preserves all fields
    assert isinstance(reparsed.payload, ToolUsePayload)
    assert reparsed.payload.tool_name == "Bash"
    assert reparsed.payload.tool_input == {"command": "ls /src"}
    assert reparsed.payload.call_id == "tu_abc"


def test_normalized_event_round_trip_tokens_consumed() -> None:
    """tokens_consumed NormalizedEvent preserves cache + thinking tokens through JSON."""
    # Given: tokens_consumed with full token breakdown
    payload = TokensConsumedPayload(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=400,
        cache_creation_input_tokens=200,
        thinking_tokens=75,
        cost_usd=0.0234,
        operation="worker_execution",
    )
    event = NormalizedEvent(
        event_id=str(uuid4()),
        run_id="test",
        occurred_at=datetime.now(timezone.utc),
        event_type="tokens_consumed",
        source="tree",
        agent_id=str(uuid4()),
        parent_agent_id=None,
        role="WORKER",
        depth=1,
        sequence_number=5,
        payload=payload,
    )

    # When: serialized and reparsed
    dumped = event.model_dump_json()
    reparsed = NormalizedEvent.model_validate_json(dumped)

    # Then: token breakdown preserved
    assert isinstance(reparsed.payload, TokensConsumedPayload)
    assert reparsed.payload.cache_read_input_tokens == 400
    assert reparsed.payload.thinking_tokens == 75


def test_run_meta_required_fields() -> None:
    """RunMeta validates with the minimum required fields populated."""
    # Given: required RunMeta fields
    # When: constructed minimally
    meta = RunMeta(
        run_id="njs.cve-2022-32414-A1-0",
        cve_id="njs.cve-2022-32414",
        cell="A1",
        replicate=0,
        system="flat_cli",
        domain_briefing_enabled=False,
        subagent_enabled=True,
        prompt_strategy="cli_default",
        docker_image="secb-tools:njs.cve-2022-32414",
        budget_usd_cap=3.0,
        wallclock_sec_cap=600,
        models={"worker": "claude-sonnet-4-6", "judge": "gpt-5"},
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        wallclock_seconds=120.5,
        termination_reason="completed",
        code_sha={"arise_sec_lion": "abc123"},
        env={"date": "2026-04-18"},
        dataset_schema_version="1.0",
    )

    # Then: validates without error
    assert meta.cell == "A1"


def test_normalized_event_rejects_naive_datetime() -> None:
    """NormalizedEvent.occurred_at requires tz-aware datetime (AwareDatetime invariant)."""
    # Given: a naive datetime (no tzinfo)
    naive = datetime(2026, 4, 18)

    # When: constructing a NormalizedEvent with the naive datetime
    # Then: Pydantic raises ValidationError because AwareDatetime rejects naive values
    with pytest.raises(ValidationError):
        NormalizedEvent(
            event_id=str(uuid4()),
            run_id="test",
            occurred_at=naive,
            event_type="tool_use",
            source="flat_cli",
            role="FLAT",
            payload=ToolUsePayload(tool_name="Bash"),
        )
