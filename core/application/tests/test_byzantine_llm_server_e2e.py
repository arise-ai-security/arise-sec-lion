"""Fast end-to-end robustness tests against a byzantine fake LLM server.

This suite validates orchestration containment against provider-level faults:
- random connection drops,
- slow token-like response pacing,
- uncancellable hangs that swallow asyncio cancellation.

The objective is run robustness only: every run must terminate (completed or
failed) within bounded wall-clock time. Output token quality is intentionally
out of scope for these tests.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import pytest

from bootstrap.application import ExecutionLimitsBridge
from bootstrap.tests.test_characterization import (
    FakeSharedContextPort,
    FakeSiblingViewPort,
    FakeWorkerTool,
    InMemoryEventStore,
    _find_child_ids,
    _get_agent_status,
)
from config import BossConfig, ManagerConfig
from core.application.agent_orchestrator import AgentOrchestrator
from core.application.execution_service import (
    AgentExecutionService,
    ExecutionServiceDependencies,
    HierarchyLimitsRegistry,
    ServiceConfig,
)
from core.application.services import (
    AgentQueryService,
    AgentRepository,
    ChildAgentFactory,
    ParentNotificationService,
    PromptBuilder,
)
from core.domain.events.events import RunCompleted, WorkFailed
from core.domain.values.llm_response import LLMResponse, LLMToolResponse, LLMUsage


class ByzantineFakeLLM:
    """Seeded fake LLM with random byzantine transport behaviors."""

    def __init__(
        self,
        *,
        seed: int,
        drop_rate: float = 0.25,
        slow_rate: float = 0.35,
        hang_rate: float = 0.20,
        hang_seconds: float = 0.40,
        fault_script: list[str] | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._drop_rate = drop_rate
        self._slow_rate = slow_rate
        self._hang_rate = hang_rate
        self._hang_seconds = hang_seconds
        self.calls = 0
        self.connection_drops = 0
        self.slow_responses = 0
        self.hangs = 0
        self.reconnect_calls = 0
        self._fault_script = list(fault_script or [])

    async def reconnect(
        self,
        *,
        model: str | None = None,
        config_dict: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> bool:
        self.reconnect_calls += 1
        await asyncio.sleep(0)
        return True

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        resp = await self.query_with_usage(prompt, config_dict)
        return resp.content

    async def query_with_usage(
        self, prompt: str, config_dict: dict[str, Any]
    ) -> LLMResponse:
        self.calls += 1
        await self._maybe_fault()
        return LLMResponse(
            content=self._response_for_prompt(prompt),
            usage=LLMUsage(prompt_tokens=16, completion_tokens=8, total_tokens=24),
            model=config_dict.get("model", "fake-byzantine"),
            cost_usd=0.0,
        )

    async def query_with_tools(
        self,
        messages: list[dict[str, Any]],
        config_dict: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        self.calls += 1
        await self._maybe_fault()
        prompt = ""
        if messages:
            prompt = str(messages[0].get("content", ""))
        return LLMToolResponse(
            content=self._response_for_prompt(prompt),
            tool_calls=[],
            usage=LLMUsage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
            model=config_dict.get("model", "fake-byzantine"),
            cost_usd=0.0,
        )

    async def _maybe_fault(self) -> None:
        scripted = self._next_scripted_fault()
        if scripted is not None:
            await self._apply_fault(scripted)
            return

        roll = self._rng.random()
        if roll < self._drop_rate:
            await self._apply_fault("drop")
            return
        if roll < self._drop_rate + self._hang_rate:
            await self._apply_fault("hang")
            return
        if roll < self._drop_rate + self._hang_rate + self._slow_rate:
            await self._apply_fault("slow")
            return
        await self._apply_fault("ok")

    def _next_scripted_fault(self) -> str | None:
        if not self._fault_script:
            return None
        return self._fault_script.pop(0)

    async def _apply_fault(self, mode: str) -> None:
        if mode == "drop":
            self.connection_drops += 1
            raise RuntimeError("Simulated LLM connection drop")
        if mode == "hang":
            self.hangs += 1
            deadline = time.monotonic() + self._hang_seconds
            while time.monotonic() < deadline:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    # Simulate stuck transport that ignores cancellation.
                    continue
            raise TimeoutError("Simulated LLM server hang")
        if mode == "slow":
            self.slow_responses += 1
            # Simulate slow token emission without making tests expensive.
            for _ in range(self._rng.randint(4, 10)):
                await asyncio.sleep(0.008)
            return
        await asyncio.sleep(0.001)

    @staticmethod
    def _response_for_prompt(prompt: str) -> str:
        if "Assess this task" in prompt:
            return '{"action":"execute","reasoning":"byzantine-test execute"}'
        return '[{"description":"do work","config":{"strategy":"heuristic","base":{"model":"gpt-4o","temperature":0.7,"max_tokens":1000},"tool":"claude_code"}}]'


def _wire_service(
    event_store: InMemoryEventStore,
    llm: ByzantineFakeLLM,
    worker: FakeWorkerTool,
    *,
    step_timeout_seconds: float = 0.20,
    pending_assessment_timeout_seconds: float = 0.30,
    max_run_duration_seconds: float = 4.0,
) -> AgentExecutionService:
    config = ServiceConfig(
        max_retries=3,
        poll_interval=0.01,
        output_directory="",
        default_worker_tool="claude_code",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        step_timeout_seconds=step_timeout_seconds,
        stall_timeout_seconds=2.0,
        worker_silence_timeout_seconds=1.0,
        no_progress_grace_seconds=0.40,
        no_progress_initial_grace_seconds=0.40,
        no_progress_check_interval=0.10,
        worker_prepare_timeout_seconds=0.30,
        worker_execute_timeout_seconds=0.50,
        pending_assessment_timeout_seconds=pending_assessment_timeout_seconds,
    )
    system_limits = ExecutionLimitsBridge(
        max_depth=-1,
        max_children_per_node=-1,
        max_total_agents=-1,
        max_concurrent_workers=-1,
        max_run_duration_seconds=max_run_duration_seconds,
    )

    prompt_builder = PromptBuilder("prompts", "claude_code")
    limits_registry = HierarchyLimitsRegistry()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    query_service = AgentQueryService(repository)
    child_factory = ChildAgentFactory(
        repository=repository,
        limits_registry=limits_registry,
        max_total_agents=-1,
        manager_config=config.manager_config,
    )
    orchestrator = AgentOrchestrator(
        llm_port=llm,
        worker_port=worker,
        prompt_builder=prompt_builder,
        child_factory=child_factory,
    )
    parent_notifier = ParentNotificationService(repository=repository)

    deps = ExecutionServiceDependencies(
        repository=repository,
        orchestrator=orchestrator,
        limits_registry=limits_registry,
        child_factory=child_factory,
        query_service=query_service,
        shared_context_port=FakeSharedContextPort(),
        sibling_view_port=FakeSiblingViewPort(),
        parent_notifier=parent_notifier,
        prompt_builder=prompt_builder,
    )
    return AgentExecutionService(
        event_store=event_store,
        dependencies=deps,
        config=config,
        system_limits=system_limits,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [5, 11, 19, 29, 37])
async def test_e2e_byzantine_llm_server_run_always_terminates(seed: int) -> None:
    """Random drop/slow/hang behavior must not deadlock the run."""
    event_store = InMemoryEventStore()
    llm = ByzantineFakeLLM(seed=seed)
    worker = FakeWorkerTool()
    service = _wire_service(event_store, llm, worker, max_run_duration_seconds=4.0)

    boss_id = await service.create_boss_agent(f"byzantine-e2e seed={seed}")
    started = time.monotonic()
    await service.run_system_loop(boss_id)
    elapsed = time.monotonic() - started

    boss_events = event_store.events_for(boss_id)
    run_completed = [e for e in boss_events if isinstance(e, RunCompleted)]
    assert run_completed, "run did not reach terminal state"
    assert elapsed < 5.0, f"run exceeded bounded time budget: {elapsed:.2f}s"
    assert _get_agent_status(event_store, boss_id) in {"completed", "failed"}

    exercised_fault = (llm.connection_drops + llm.slow_responses + llm.hangs) > 0
    assert exercised_fault, "seed did not exercise any byzantine behavior"


@pytest.mark.asyncio
async def test_e2e_hung_assessment_is_reaped_by_pending_timeout() -> None:
    """Cancellation-resistant assessment hang is contained by pending watchdog."""
    event_store = InMemoryEventStore()
    llm = ByzantineFakeLLM(
        seed=123,
        # First assessment succeeds so boss can spawn the child; all later
        # calls hang to exercise the child PENDING watchdog path.
        fault_script=["ok", "hang", "hang", "hang", "hang"],
        drop_rate=0.0,
        slow_rate=0.0,
        hang_rate=0.0,
        hang_seconds=0.30,
    )
    worker = FakeWorkerTool()
    service = _wire_service(
        event_store,
        llm,
        worker,
        step_timeout_seconds=0.10,
        pending_assessment_timeout_seconds=0.15,
        max_run_duration_seconds=2.0,
    )

    boss_id = await service.create_boss_agent("pending timeout containment")
    # Pre-drive boss so the loop starts with a PENDING child as the active unit.
    await service.run_agent_step(boss_id)
    child_id = _find_child_ids(event_store, boss_id)[0]
    await service.run_system_loop(boss_id)

    boss_events = event_store.events_for(boss_id)
    assert any(isinstance(e, RunCompleted) for e in boss_events)
    # At least one assessment-timeout failure reason should appear in the run.
    fail_reasons = [
        e.reason
        for e in event_store.all_events_flat()
        if isinstance(e, WorkFailed) and e.reason
    ]
    assert any(
        r.startswith("Pending assessment timed out after") for r in fail_reasons
    ), fail_reasons
    assert _get_agent_status(event_store, child_id) == "failed"
