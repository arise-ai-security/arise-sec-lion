"""Bootstrap-level retry wiring and retry-context tests."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import UUID

import pytest

from bootstrap.composition import create_runtime_cli
from bootstrap.infrastructure import Infrastructure
from bootstrap.tests.test_characterization import (
    FakeLLM,
    FakeSharedContextPort,
    FakeSiblingViewPort,
    InMemoryEventStore,
    _find_child_ids,
    _get_agent_status,
)
from config import Settings
from core.domain.events.events import CodeGenerationStarted, DomainEvent, PromptSent, RetryScheduled, WorkCompleted, WorkFailed


class _NoopReconTool:
    @property
    def name(self) -> str:
        return "recon"

    def get_tool_definitions(self) -> list[dict]:
        return []

    async def execute_tool(self, name: str, arguments: dict) -> str:
        raise AssertionError(f"Unexpected tool call: {name} {arguments}")


class FailThenSucceedWorker:
    """Worker that fails once, then succeeds on the retry."""

    def __init__(self) -> None:
        self.call_count = 0

    async def run_session(self, task_context: dict) -> AsyncIterator[DomainEvent]:
        self.call_count += 1
        agent_id: UUID = task_context["agent_id"]

        yield CodeGenerationStarted(
            aggregate_id=agent_id,
            sequence_number=0,
            tool_name="fake",
        )

        if self.call_count == 1:
            yield WorkFailed(
                aggregate_id=agent_id,
                sequence_number=0,
                reason="Transient worker failure",
            )
            return

        yield WorkCompleted(
            aggregate_id=agent_id,
            sequence_number=0,
            result="Recovered after retry",
        )


@pytest.mark.asyncio
async def test_create_runtime_cli_wires_retry_config_and_retry_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    settings = Settings.from_yaml(
        "/Users/garfield/PycharmProjects/arise-sec-lion/config/config.yaml"
    )

    event_store = InMemoryEventStore()
    llm = FakeLLM()
    worker = FailThenSucceedWorker()
    shared_context = FakeSharedContextPort()
    fake_infra = Infrastructure(
        event_store=event_store,
        llm_adapter=llm,
        worker_tool=worker,
        shared_context=shared_context,
        recon_tool=_NoopReconTool(),
        secbench_runtime=SimpleNamespace(),
    )

    monkeypatch.setattr(
        "bootstrap.composition.get_infrastructure",
        lambda _config: fake_infra,
    )

    cli = create_runtime_cli(settings)
    retry_config = cli.execution_service._retry_config

    assert retry_config is not None
    assert retry_config.model_escalation_chain == ["openai/gpt-4-turbo"]

    boss_id = await cli.execution_service.create_boss_agent("Bootstrap retry wiring test")
    await cli.execution_service.run_agent_step(boss_id)
    child_id = _find_child_ids(event_store, boss_id)[0]

    await cli.execution_service.run_agent_step(child_id)
    await cli.execution_service.run_agent_step(child_id)

    retry_events = [
        event for event in event_store.events_for(child_id)
        if isinstance(event, RetryScheduled)
    ]
    assert len(retry_events) == 1
    assert _get_agent_status(event_store, child_id) == "analyzing"

    await cli.execution_service.run_agent_step(child_id)

    prompt_events = [
        event for event in event_store.events_for(child_id)
        if isinstance(event, PromptSent) and event.prompt_type == "worker_execution"
    ]
    assert len(prompt_events) == 2
    assert "<previous_attempt_feedback>" in prompt_events[-1].prompt
    assert "Transient worker failure" in prompt_events[-1].prompt
    assert _get_agent_status(event_store, child_id) == "completed"
