"""Worker cost-provenance on timeout/cancellation.

Verifies the two adapters honor the provenance rule: harvest SDK usage before
shutdown when available, emit partial usage with ``complete=False``, never
assign zero cost silently, and mark the run ``usage_missing`` when provider
usage cannot be recovered.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.domain.events.events import WorkerCostRecorded, WorkFailed
from infrastructure.adapters.worker import claude_sdk_adapter
from infrastructure.adapters.worker.claude_sdk_adapter import (
    ClaudeAgentSDKAdapter,
    SDKAdapterConfig,
)
from infrastructure.adapters.worker.google_adk_adapter import (
    ADKAdapterConfig,
    GoogleADKAdapter,
)
from infrastructure.adapters.worker.shared.cost_calculator import get_model_pricing


def _only_cost_event(events: list) -> WorkerCostRecorded:
    costs = [e for e in events if isinstance(e, WorkerCostRecorded)]
    assert len(costs) == 1, f"expected exactly one cost event, got {events}"
    return costs[0]


class _FakeAdkUsage:
    def __init__(self, prompt: int, candidates: int) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class _FakeAdkEvent:
    """Minimal ADK event carrying usage but no content."""

    def __init__(self, prompt: int, candidates: int) -> None:
        self.usage_metadata = _FakeAdkUsage(prompt, candidates)
        self.content = None


class _FakeSessionService:
    async def create_session(self, **_kwargs: object) -> None:
        return None


class _FakeRunner:
    """Streams two usage-bearing events, then hangs to force a timeout."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def run_async(self, *, user_id: str, session_id: str, new_message: object):
        yield _FakeAdkEvent(prompt=1000, candidates=500)
        yield _FakeAdkEvent(prompt=200, candidates=100)
        await asyncio.sleep(10)


class TestGoogleAdkTimeoutCostProvenance:
    """Gap 2: harvested tokens on timeout must yield real (non-zero) cost."""

    @pytest.mark.asyncio
    async def test_timeout_with_harvested_tokens_reports_nonzero_cost(self) -> None:
        """Google timeout after streamed usage reports non-zero cost, complete=False."""
        # Given: an ADK worker whose run streams two usage-bearing events then hangs.
        adapter = GoogleADKAdapter(
            ADKAdapterConfig(
                model="gemini-3-pro",
                timeout_seconds=0.05,  # type: ignore[arg-type]
                allowed_tools=[],  # skip McpToolset/shell tool construction
            )
        )

        # When: the session runs and is bounded by the adapter timeout.
        with (
            patch("google.adk.agents.LlmAgent", MagicMock()),
            patch("google.adk.sessions.InMemorySessionService", _FakeSessionService),
            patch("google.adk.runners.Runner", _FakeRunner),
        ):
            events = [
                event
                async for event in adapter.run_session(
                    {"task_description": "solve", "agent_id": uuid4()}
                )
            ]

        # Then: the terminal event marks a timeout failure.
        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason

        # And: the cost event carries the harvested tokens with a real cost.
        cost = _only_cost_event(events)
        expected_cost = get_model_pricing("gemini-3-pro").calculate_cost(1200, 600)
        assert expected_cost > 0.0
        assert cost.complete is False
        assert cost.termination_reason == "timeout"
        assert cost.prompt_tokens == 1200
        assert cost.completion_tokens == 600
        assert cost.cost_usd == pytest.approx(expected_cost)
        assert cost.usage_missing is False


async def _async_iter(items):
    for item in items:
        yield item


def _hanging_sdk_client() -> MagicMock:
    async def _hang(_message: str) -> None:
        await asyncio.sleep(10)

    mock_client = AsyncMock()
    mock_client.query = AsyncMock(side_effect=_hang)
    mock_client.receive_response = MagicMock(return_value=_async_iter([]))

    mock_client_class = MagicMock()
    mock_client_class.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_class.__aexit__ = AsyncMock(return_value=None)
    return mock_client_class


class TestClaudeSdkTimeoutCostProvenance:
    """Gap 1: no pre-terminal usage exists in this SDK, so mark explicit-incomplete.

    The installed ``claude_agent_sdk`` exposes usage only on the terminal
    ``ResultMessage`` (``AssistantMessage`` has no ``usage`` field, and
    ``receive_response`` stops at the first ``ResultMessage``). A timeout means
    no ``ResultMessage`` was ever received, so there is nothing to harvest. This
    locks in the required fallback: ``complete=False`` + ``usage_missing=True``
    without silently reporting a fabricated non-zero cost.
    """

    @pytest.mark.asyncio
    async def test_timeout_marks_usage_incomplete_without_silent_zero(self) -> None:
        """Claude timeout emits an explicit-incomplete cost event, not a silent zero."""
        # Given: an adapter whose SDK query hangs past the timeout.
        adapter = ClaudeAgentSDKAdapter(
            SDKAdapterConfig(
                model="claude-sonnet-4-20250514",
                timeout_seconds=0.01,  # type: ignore[arg-type]
            )
        )

        # When: the session runs and the query never yields a ResultMessage.
        with patch.object(
            claude_sdk_adapter, "ClaudeSDKClient", return_value=_hanging_sdk_client()
        ):
            events = [
                event
                async for event in adapter.run_session(
                    {"task_description": "solve", "agent_id": uuid4()}
                )
            ]

        # Then: the terminal event marks a timeout failure.
        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason

        # And: the cost event marks the run incomplete without a silent zero.
        cost = _only_cost_event(events)
        assert cost.complete is False
        assert cost.termination_reason == "timeout"
        assert cost.usage_missing is True
        assert cost.cost_usd == 0.0
        assert cost.tokens is None
