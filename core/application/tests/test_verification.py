"""Tests for the verification pipeline (Phase 3).

Tests the 4-stage verification: structural → deterministic → execution → judge.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.services import PromptBuilder
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    CodeGenerationStarted,
    DomainEvent,
    VerificationFailed,
    WorkCompleted,
    WorkFailed,
)
from core.domain.values.llm_response import LLMResponse, LLMUsage


def _config() -> dict[str, Any]:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


class FakeLLM:
    """Fake LLM for verification tests."""

    def __init__(self, judge_response: dict | None = None) -> None:
        self._judge_response = judge_response or {"score": 85, "feedback": "ok"}
        self.calls: list[str] = []

    async def query(self, prompt: str, config_dict: dict) -> str:
        return json.dumps(self._judge_response)

    async def query_with_usage(self, prompt: str, config_dict: dict) -> LLMResponse:
        self.calls.append(prompt)
        return LLMResponse(
            content=json.dumps(self._judge_response),
            usage=LLMUsage(prompt_tokens=50, completion_tokens=25, total_tokens=75),
            model="gpt-4o",
            cost_usd=0.01,
        )


class FakeWorkerTool:
    """Fake worker that yields configurable events."""

    def __init__(self, result: str = "Task done", fail: bool = False) -> None:
        self._result = result
        self._fail = fail

    async def run_session(self, task_context: dict) -> AsyncIterator[DomainEvent]:
        agent_id = task_context["agent_id"]
        yield CodeGenerationStarted(
            aggregate_id=agent_id, sequence_number=0, tool_name="fake"
        )
        if self._fail:
            yield WorkFailed(
                aggregate_id=agent_id, sequence_number=0, reason="tool error"
            )
        else:
            yield WorkCompleted(
                aggregate_id=agent_id, sequence_number=0, result=self._result
            )


def _make_worker_agent(success_criteria: str = "") -> AgentSession:
    """Create a WORKER agent ready for execution."""

    agent_id = uuid4()
    agent = AgentSession.create(
        agent_id=agent_id,
        role=AgentRole.PENDING,
        config=_config(),
        parent_id=uuid4(),
        success_criteria=success_criteria,
    )
    agent.assign_task("Do the thing")
    agent.apply_complexity_result(
        complexity="simple", reasoning="simple task", determined_role=AgentRole.WORKER
    )
    agent.mark_changes_as_committed()
    return agent


def _make_orchestrator(
        llm: FakeLLM | None = None,
        worker: FakeWorkerTool | None = None,
) -> AgentOrchestrator:
    from unittest.mock import MagicMock

    return AgentOrchestrator(
        llm_port=llm or FakeLLM(),
        worker_port=worker or FakeWorkerTool(),
        prompt_builder=PromptBuilder("prompts", "claude_code"),
        child_factory=MagicMock(),
    )


class TestVerificationStructural:
    """Stage 1: Structural check."""

    @pytest.mark.asyncio
    async def test_empty_result_fails_structural(self) -> None:
        """Worker producing empty output fails structural verification."""
        worker = FakeWorkerTool(result="")
        orchestrator = _make_orchestrator(worker=worker)
        agent = _make_worker_agent()

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.FAILED
        verification_events = [e for e in agent.events if isinstance(e, VerificationFailed)]
        assert len(verification_events) == 1
        assert verification_events[0].failed_stage == "structural"

    @pytest.mark.asyncio
    async def test_whitespace_only_fails_structural(self) -> None:
        worker = FakeWorkerTool(result="   \n\t  ")
        orchestrator = _make_orchestrator(worker=worker)
        agent = _make_worker_agent()

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.FAILED
        verification_events = [e for e in agent.events if isinstance(e, VerificationFailed)]
        assert verification_events[0].failed_stage == "structural"

    @pytest.mark.asyncio
    async def test_non_empty_result_passes_structural(self) -> None:
        worker = FakeWorkerTool(result="Valid output")
        orchestrator = _make_orchestrator(worker=worker)
        agent = _make_worker_agent()

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED


class TestVerificationJudge:
    """Stage 4: LLM Judge."""

    @pytest.mark.asyncio
    async def test_judge_passes(self) -> None:
        llm = FakeLLM(judge_response={"score": 85, "feedback": "looks good"})
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="Output must contain valid code")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_judge_fails(self) -> None:
        llm = FakeLLM(judge_response={"score": 30, "feedback": "Missing tests"})
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="Must include unit tests")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.FAILED
        verification_events = [e for e in agent.events if isinstance(e, VerificationFailed)]
        assert len(verification_events) == 1
        assert verification_events[0].failed_stage == "judge"
        assert "Missing tests" in verification_events[0].feedback
        assert verification_events[0].score == 30
        assert "structural" in verification_events[0].stages_passed

    @pytest.mark.asyncio
    async def test_judge_threshold_60_passes(self) -> None:
        """Score of exactly 60 should pass (threshold is 60)."""
        llm = FakeLLM(judge_response={"score": 60, "feedback": "barely acceptable"})
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="Must do the work")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_judge_score_59_fails(self) -> None:
        """Score of 59 should fail (below threshold of 60)."""
        llm = FakeLLM(judge_response={"score": 59, "feedback": "not enough"})
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="Must do the work")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.FAILED

    @pytest.mark.asyncio
    async def test_judge_backward_compat_passed_true(self) -> None:
        """Old-style {"passed": true} response maps to score 100."""
        llm = FakeLLM(judge_response={"passed": True, "feedback": "looks good"})
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="Output must be valid")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_no_criteria_skips_judge(self) -> None:
        """Without success_criteria, judge stage is skipped entirely."""
        llm = FakeLLM(judge_response={"passed": False, "feedback": "fail"})
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="")

        await orchestrator.execute_task(agent)

        # Should pass because judge is skipped
        assert agent.status == AgentStatus.COMPLETED
        # LLM should not have been called for judging
        assert not any("quality judge" in c.lower() for c in llm.calls)

    @pytest.mark.asyncio
    async def test_judge_parse_failure_fails_verification(self) -> None:
        """If judge response can't be parsed, verification fails (don't silently pass)."""
        llm = FakeLLM()
        llm._judge_response = "not json at all"  # type: ignore
        orchestrator = _make_orchestrator(llm=llm)
        agent = _make_worker_agent(success_criteria="Some criteria")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.FAILED

    @pytest.mark.asyncio
    async def test_judge_prompt_strips_ansi_sequences(self) -> None:
        llm = FakeLLM(judge_response={"passed": True, "feedback": "clean output"})
        worker = FakeWorkerTool(result="\x1b[31mTask completed successfully\x1b[0m\nnext line")
        orchestrator = _make_orchestrator(llm=llm, worker=worker)
        agent = _make_worker_agent(success_criteria="Must report task completion")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED
        assert llm.calls
        assert "\x1b[" not in llm.calls[-1]
        assert "Task completed successfully" in llm.calls[-1]


class TestVerificationSkipsOnWorkerFailure:
    """Verification only runs on successful worker completion."""

    @pytest.mark.asyncio
    async def test_failed_worker_not_verified(self) -> None:
        worker = FakeWorkerTool(fail=True)
        orchestrator = _make_orchestrator(worker=worker)
        agent = _make_worker_agent(success_criteria="Must work")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.FAILED
        # Should be WorkFailed, not VerificationFailed
        verification_events = [e for e in agent.events if isinstance(e, VerificationFailed)]
        assert len(verification_events) == 0


class TestJudgeLargeOutput:
    """Verify the judge sees enough of large worker output to make correct decisions."""

    @pytest.mark.asyncio
    async def test_judge_sees_tail_of_large_output(self) -> None:
        """Key evidence at the end of a large output must reach the judge.

        This is the A1 fix: previously truncated to 3000 chars, now uses
        head+tail with 12000 char budget.
        """
        # Build a large output where the proof of success is at the very end
        padding = "Analyzing source code...\n" * 500  # ~12K chars of filler
        evidence = "\n[SUCCESS] Output validation: expected pattern confirmed"
        large_output = padding + evidence

        # Judge checks for the evidence keyword in the output
        class EvidenceCheckingLLM(FakeLLM):
            async def query_with_usage(self, prompt: str, config_dict: dict) -> LLMResponse:
                self.calls.append(prompt)
                # If judge can see the evidence, it passes
                if "expected pattern confirmed" in prompt:
                    verdict = {"score": 90, "feedback": "Task completed"}
                else:
                    verdict = {"score": 20, "feedback": "No evidence of completion"}
                return LLMResponse(
                    content=json.dumps(verdict),
                    usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                    model="gpt-4o",
                    cost_usd=0.01,
                )

        llm = EvidenceCheckingLLM()
        worker = FakeWorkerTool(result=large_output)
        orchestrator = _make_orchestrator(llm=llm, worker=worker)
        agent = _make_worker_agent(success_criteria="Must produce expected output pattern")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED, (
            "Judge should see evidence at tail of large output"
        )

    @pytest.mark.asyncio
    async def test_judge_sees_head_of_large_output(self) -> None:
        """Key evidence at the start of a large output must also reach the judge."""
        evidence = "[PATCH APPLIED] Fixed parsing error in process_request()\n"
        padding = "Running verification tests...\n" * 500
        large_output = evidence + padding

        class EvidenceCheckingLLM(FakeLLM):
            async def query_with_usage(self, prompt: str, config_dict: dict) -> LLMResponse:
                self.calls.append(prompt)
                if "Fixed parsing error" in prompt:
                    verdict = {"score": 90, "feedback": "Patch applied"}
                else:
                    verdict = {"score": 20, "feedback": "No patch found"}
                return LLMResponse(
                    content=json.dumps(verdict),
                    usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                    model="gpt-4o",
                    cost_usd=0.01,
                )

        llm = EvidenceCheckingLLM()
        worker = FakeWorkerTool(result=large_output)
        orchestrator = _make_orchestrator(llm=llm, worker=worker)
        agent = _make_worker_agent(success_criteria="Must apply patch")

        await orchestrator.execute_task(agent)

        assert agent.status == AgentStatus.COMPLETED
