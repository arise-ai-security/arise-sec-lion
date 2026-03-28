"""Tests for assessment parse-retry logic in AgentOrchestrator."""

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.application.agent_orchestrator import (
    _ASSESSMENT_PARSE_RETRIES,
    AgentOrchestrator,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.values.agent_config import AgentConfig
from core.domain.values.hierarchy_limits import HierarchyLimits
from core.domain.values.llm_response import LLMResponse, LLMUsage


VALID_EXECUTE_JSON = json.dumps({"action": "execute", "reasoning": "Simple task"})
MALFORMED_JSON = '{"action": "execute", "reasoning": "truncated...'

_USAGE = LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)


def _make_response(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage=_USAGE, model="test", cost_usd=0.001)


def _make_pending_agent() -> AgentSession:
    root_id = uuid4()
    agent = AgentSession.create(
        agent_id=uuid4(),
        parent_id=root_id,
        role=AgentRole.PENDING,
        task_description="Fix the bug",
        config=AgentConfig(
            strategy="heuristic",
            base=AgentConfig.BaseConfig(
                model="gpt-4o-mini", temperature=0.5, max_tokens=4000
            ),
        ),
        hierarchy_limits=HierarchyLimits(
            root_id=root_id,
            current_depth=1,
            max_depth=3,
            max_children_per_node=5,
            max_total_agents=20,
            current_total_agents=2,
        ),
    )
    agent.clear_changes()
    return agent


def _build_orchestrator(llm_mock: AsyncMock) -> AgentOrchestrator:
    prompt_builder = MagicMock()
    prompt_builder.build_assessment_prompt.return_value = "fake prompt"
    child_factory = MagicMock()
    return AgentOrchestrator(
        llm_port=llm_mock,
        worker_port=MagicMock(),
        prompt_builder=prompt_builder,
        child_factory=child_factory,
    )


@pytest.fixture
def llm():
    return AsyncMock()


class TestAssessmentRetry:
    """Verify that assess_task retries on malformed JSON before failing."""

    async def test_succeeds_on_second_attempt(self, llm):
        """First LLM call returns garbage, second returns valid JSON."""
        llm.query_with_usage = AsyncMock(
            side_effect=[
                _make_response(MALFORMED_JSON),
                _make_response(VALID_EXECUTE_JSON),
            ]
        )
        orchestrator = _build_orchestrator(llm)
        agent = _make_pending_agent()

        await orchestrator.assess_task(agent)

        assert agent.status == AgentStatus.EXECUTING
        assert agent.role == AgentRole.WORKER
        assert llm.query_with_usage.call_count == 2

    async def test_fails_after_all_retries_exhausted(self, llm):
        """All attempts return malformed JSON → agent fails."""
        llm.query_with_usage = AsyncMock(
            return_value=_make_response(MALFORMED_JSON),
        )
        orchestrator = _build_orchestrator(llm)
        agent = _make_pending_agent()

        await orchestrator.assess_task(agent)

        assert agent.status == AgentStatus.FAILED
        assert f"after {_ASSESSMENT_PARSE_RETRIES} attempts" in (
            agent.error_message or ""
        )
        assert llm.query_with_usage.call_count == _ASSESSMENT_PARSE_RETRIES

    async def test_no_retry_on_first_success(self, llm):
        """Valid JSON on first try → no retry, single call."""
        llm.query_with_usage = AsyncMock(
            return_value=_make_response(VALID_EXECUTE_JSON),
        )
        orchestrator = _build_orchestrator(llm)
        agent = _make_pending_agent()

        await orchestrator.assess_task(agent)

        assert agent.status == AgentStatus.EXECUTING
        assert llm.query_with_usage.call_count == 1
