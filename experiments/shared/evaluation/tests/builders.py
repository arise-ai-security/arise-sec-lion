"""Synthetic event + run builders for evaluation tests.

Keeps tests DB-free: a ``RunBuilder`` assembles real ``DomainEvent`` instances
(per-aggregate sequence numbers, monotonic timestamps) and a ``RunData`` over a
temp directory, mirroring what ``load_run`` produces.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 — used as a runtime fixture parameter type
from uuid import UUID, uuid4

from core.domain.events.events import (
    AgentCreated,
    AgentExecutionStarted,
    ChildSpawned,
    ComplexityEvaluated,
    OperationStarted,
    PromptSent,
    SourceFileEdited,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    VerificationFailed,
    VerificationPassed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from core.domain.values.subtask import Subtask
from experiments.shared.evaluation.models import RunData


_BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def tool_use_content(action: str, payload: dict[str, object]) -> str:
    """Reproduce the OpenHands producer format: ``Tool: X\\nInput: {json}``."""
    return f"Tool: {action}\nInput: {json.dumps(payload)}"


def file_editor(command: str, path: str = "/testcase/poc") -> tuple[str, str]:
    """A FileEditorAction tool-use (action, content)."""
    return "FileEditorAction", tool_use_content(
        "FileEditorAction", {"command": command, "path": path}
    )


def shell(command: str = "ls -la /testcase") -> tuple[str, str]:
    """An MCPToolAction shell tool-use, including the nested repr fallback."""
    repr_str = f"data={{'command': {command!r}}} kind='MCPToolAction'"
    return "MCPToolAction", tool_use_content("MCPToolAction", {"repr": repr_str})


def mcp_other() -> tuple[str, str]:
    """An MCPToolAction with no shell command (e.g. a direct valgrind/klee call)."""
    return "MCPToolAction", tool_use_content(
        "MCPToolAction", {"repr": "data={'binary': 'x'} kind='MCPToolAction'"}
    )


def grep(pattern: str = "needle") -> tuple[str, str]:
    """A GrepAction tool-use."""
    return "GrepAction", tool_use_content("GrepAction", {"pattern": pattern, "path": "/src"})


def glob(pattern: str = "*.c") -> tuple[str, str]:
    """A GlobAction tool-use."""
    return "GlobAction", tool_use_content("GlobAction", {"pattern": pattern})


class RunBuilder:
    """Accumulates events with per-aggregate sequence numbers and timestamps."""

    def __init__(self, run_id: UUID | None = None) -> None:
        self.run_id = run_id or uuid4()
        self.events: list = []
        self._seq: dict[UUID, int] = {}
        self._clock = itertools.count()

    def _add(self, aggregate_id: UUID, event_cls, **kwargs):
        seq = self._seq.get(aggregate_id, 0)
        self._seq[aggregate_id] = seq + 1
        event = event_cls(
            aggregate_id=aggregate_id,
            sequence_number=seq,
            occurred_at=_BASE_TS + timedelta(seconds=next(self._clock)),
            **kwargs,
        )
        self.events.append(event)
        return event

    # -- node lifecycle ----------------------------------------------------

    def boss(self, *, success_criteria: str = "") -> UUID:
        """Create the boss/root agent; returns its id (== run_id)."""
        self._add(
            self.run_id,
            AgentCreated,
            role="boss",
            parent_id=None,
            success_criteria=success_criteria,
        )
        return self.run_id

    def agent(
        self,
        role: str,
        parent_id: UUID,
        task: str,
        *,
        agent_id: UUID | None = None,
        success_criteria: str = "",
    ) -> UUID:
        """Create a non-root agent with an AgentCreated + TaskAssigned + ChildSpawned."""
        agent_id = agent_id or uuid4()
        self._add(
            agent_id,
            AgentCreated,
            role=role,
            parent_id=parent_id,
            success_criteria=success_criteria,
        )
        self._add(agent_id, TaskAssigned, task_description=task)
        self._add(
            parent_id,
            ChildSpawned,
            child_id=agent_id,
            child_role=role,
            subtask=Subtask(description=task, config={}),
            child_config={},
        )
        return agent_id

    def assessed_agent(
        self, determined_role: str, parent_id: UUID, task: str, *, agent_id: UUID | None = None
    ) -> UUID:
        """Model the real flow: created as ``pending``, then assessed into a role."""
        agent_id = agent_id or uuid4()
        self._add(agent_id, AgentCreated, role="pending", parent_id=parent_id)
        self._add(agent_id, TaskAssigned, task_description=task)
        self._add(
            agent_id, ComplexityEvaluated, complexity="complex", determined_role=determined_role
        )
        self._add(
            parent_id,
            ChildSpawned,
            child_id=agent_id,
            child_role=determined_role,
            subtask=Subtask(description=task, config={}),
            child_config={},
        )
        return agent_id

    def exec_started(self, agent_id: UUID, role: str) -> None:
        self._add(agent_id, AgentExecutionStarted, role=role, depth=1)

    # -- per-agent activity ------------------------------------------------

    def op_started(self, agent_id: UUID, op: str = "worker_execution") -> None:
        self._add(agent_id, OperationStarted, operation_type=op)

    def prompt(
        self,
        agent_id: UUID,
        text: str,
        *,
        prompt_type: str = "worker_execution",
        target: str = "openhands",
    ) -> None:
        self._add(agent_id, PromptSent, prompt=text, prompt_type=prompt_type, target=target)

    def tool(self, agent_id: UUID, action_content: tuple[str, str]) -> None:
        action, content = action_content
        self._add(
            agent_id, ThoughtCaptured, content=content, output_type="tool_use", tool_name=action
        )

    def thought(self, agent_id: UUID, text: str) -> None:
        """A reasoning thought (tool_use with null tool_name) — NOT a tool call."""
        self._add(
            agent_id, ThoughtCaptured, content=text, output_type="tool_use", tool_name=None
        )

    def tool_result(self, agent_id: UUID, text: str) -> None:
        """A tool result row (response side) — NOT a tool call."""
        self._add(
            agent_id, ThoughtCaptured, content=text, output_type="tool_result", tool_name=None
        )

    def finish(self, agent_id: UUID) -> None:
        self._add(
            agent_id,
            ThoughtCaptured,
            content=tool_use_content("FinishAction", {"thought": "done"}),
            output_type="tool_use",
            tool_name="FinishAction",
        )

    def tokens(
        self,
        agent_id: UUID,
        *,
        cost: float,
        prompt: int = 0,
        cache_read: int = 0,
        operation: str = "task_decomposition",
    ) -> None:
        self._add(
            agent_id,
            TokensConsumed,
            model="claude-sonnet-4-6",
            prompt_tokens=prompt,
            completion_tokens=0,
            total_tokens=prompt,
            cache_read_tokens=cache_read,
            cost_usd=cost,
            operation=operation,
        )

    def worker_cost(
        self, agent_id: UUID, *, cost: float, prompt: int = 0, cache_read: int = 0
    ) -> None:
        self._add(
            agent_id,
            WorkerCostRecorded,
            tool_name="openhands",
            model="gpt-5.4-mini",
            prompt_tokens=prompt,
            cache_read_tokens=cache_read,
            cost_usd=cost,
        )

    def edited(self, agent_id: UUID, path: str) -> None:
        self._add(agent_id, SourceFileEdited, path=path, edited_by=str(agent_id))

    def completed(self, agent_id: UUID, result: str) -> None:
        self._add(agent_id, WorkCompleted, result=result)

    def failed(self, agent_id: UUID, reason: str) -> None:
        self._add(agent_id, WorkFailed, reason=reason)

    def verification_passed(self, agent_id: UUID) -> None:
        self._add(agent_id, VerificationPassed)

    def verification_failed(
        self, agent_id: UUID, *, stage: str = "structural", score: int = 0
    ) -> None:
        self._add(agent_id, VerificationFailed, failed_stage=stage, feedback="bad", score=score)

    # -- materialize -------------------------------------------------------

    def run_data(self, run_dir: Path, *, manifest: dict | None = None) -> RunData:
        """Build a RunData with events sorted like ``load_run`` does."""
        events = sorted(
            self.events, key=lambda e: (e.occurred_at, str(e.aggregate_id), e.sequence_number)
        )
        return RunData(
            run_id=self.run_id, events=events, run_dir=run_dir, manifest=manifest or {}
        )


def write_files(run_dir: Path, files: dict[str, bytes]) -> Path:
    """Write container-path files under ``run_dir`` (``/testcase/x`` → run_dir/testcase/x)."""
    for container_path, content in files.items():
        disk = run_dir / container_path.lstrip("/")
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(content)
    return run_dir
