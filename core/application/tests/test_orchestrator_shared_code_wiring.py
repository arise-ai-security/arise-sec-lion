"""AgentOrchestrator wiring of the shared code-prefix block into worker prompts.

When a ``SharedCodeContextPort`` is injected, ``execute_task`` must rebuild the
run's block via ``code_block(root_id)`` and feed it to ``build_worker_prompt``,
and must thread ``root_id`` into the worker ``task_context`` so the adapter can
capture views/edits. When no port is injected, neither happens (off => today).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.application.agent_orchestrator import AgentOrchestrator
from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import DomainEvent
from core.domain.values.limits import HierarchyLimits


_CONFIG = {
    "strategy": "heuristic",
    "base": {"model": "gpt-4", "temperature": 0.0, "max_tokens": 1000},
    "tool": "openhands",
}


class _SpyPromptBuilder:
    """Records the shared_code_block passed to build_worker_prompt."""

    def __init__(self) -> None:
        self.shared_code_block: str | None = None
        self.calls = 0

    def build_worker_prompt(self, **kwargs: Any) -> str:
        self.calls += 1
        self.shared_code_block = kwargs.get("shared_code_block")
        return "WORKER PROMPT"


class _CapturingWorkerPort:
    """Worker port that records task_context and yields no events."""

    def __init__(self) -> None:
        self.task_context: dict[str, Any] | None = None

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        self.task_context = task_context
        return
        yield  # type: ignore[unreachable]


class _FakeSharedCodePort:
    def __init__(self, block: str | None) -> None:
        self.block = block
        self.code_block_calls: list[UUID] = []

    async def record_view(self, root_id: UUID, agent_id: UUID, path: str, content: str) -> None:
        return None

    async def record_edit(self, root_id: UUID, agent_id: UUID, path: str) -> None:
        return None

    async def code_block(self, root_id: UUID) -> str | None:
        self.code_block_calls.append(root_id)
        return self.block


def _worker_agent(root_id: UUID) -> AgentSession:
    agent = AgentSession.create(
        agent_id=uuid4(), role=AgentRole.WORKER, config=_CONFIG, parent_id=root_id
    )
    agent.set_hierarchy_limits(
        HierarchyLimits.create_root(
            root_id=root_id, max_depth=3, max_children_per_node=3, max_retries=1
        )
    )
    agent.mark_changes_as_committed()
    return agent


def _orchestrator(
    *, prompt_builder: _SpyPromptBuilder, worker_port: _CapturingWorkerPort, shared_code_port: Any
) -> AgentOrchestrator:
    # child_factory / llm_port are unused on the execute_task path taken here.
    return AgentOrchestrator(
        llm_port=object(),  # type: ignore[arg-type]
        worker_port=worker_port,  # type: ignore[arg-type]
        prompt_builder=prompt_builder,  # type: ignore[arg-type]
        child_factory=object(),  # type: ignore[arg-type]
        shared_code_port=shared_code_port,
    )


@pytest.mark.asyncio
async def test_execute_task_injects_block_and_threads_root_id() -> None:
    # Given: a WORKER agent and an injected shared-code port returning a block
    root_id = uuid4()
    agent = _worker_agent(root_id)
    builder = _SpyPromptBuilder()
    worker_port = _CapturingWorkerPort()
    shared_port = _FakeSharedCodePort(block="<provided_source_files>...</provided_source_files>")
    orchestrator = _orchestrator(
        prompt_builder=builder, worker_port=worker_port, shared_code_port=shared_port
    )

    # When
    await orchestrator.execute_task(agent)

    # Then: the block was rebuilt for this run and fed to the worker prompt builder,
    # and root_id was threaded into the worker task_context for capture.
    assert shared_port.code_block_calls == [root_id]
    assert builder.shared_code_block == "<provided_source_files>...</provided_source_files>"
    assert worker_port.task_context is not None
    assert worker_port.task_context["root_id"] == root_id


@pytest.mark.asyncio
async def test_execute_task_without_port_passes_none_block() -> None:
    # Given: the same agent but NO shared-code port injected
    root_id = uuid4()
    agent = _worker_agent(root_id)
    builder = _SpyPromptBuilder()
    worker_port = _CapturingWorkerPort()
    orchestrator = _orchestrator(
        prompt_builder=builder, worker_port=worker_port, shared_code_port=None
    )

    # When
    await orchestrator.execute_task(agent)

    # Then: no block is injected (byte-identical to today)
    assert builder.shared_code_block is None
