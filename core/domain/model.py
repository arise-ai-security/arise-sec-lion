"""Core domain models for the multi-agent system."""

import json
from enum import Enum
from functools import singledispatchmethod
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from core.domain.agent_config import AgentConfig
from core.domain.config_resolver import ConfigResolver
from core.domain.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    WorkCompleted,
    WorkFailed,
)
from core.domain.exceptions import ToolNotAvailableError
from core.domain.prompt_builder import PromptBuilder
from core.domain.services import SubtaskParser, strip_markdown_code_block
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort


class AgentRole(str, Enum):
    """Agent role: BOSS (root), PENDING (awaiting eval), MANAGER (decomposes), WORKER (executes)."""

    BOSS = "boss"
    PENDING = "pending"
    MANAGER = "manager"
    WORKER = "worker"


class AgentStatus(str, Enum):
    """Agent execution status."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class AgentSession:
    """Event-sourced aggregate for agent sessions. State derived from replaying events."""

    def __init__(self, session_id: UUID) -> None:
        """Internal. Use AgentSession.create() or load_from_history() instead."""
        self._initialize_defaults(session_id)

    @classmethod
    def create(
        cls,
        session_id: UUID,
        role: AgentRole,
        config: dict[str, Any],
        parent_id: UUID | None = None,
    ) -> "AgentSession":
        instance = cls(session_id)
        event = AgentCreated(
            aggregate_id=session_id,
            sequence_number=instance._next_sequence(),
            role=role.value,
            parent_id=parent_id,
            config=config,
        )
        instance._apply(event)
        instance._changes.append(event)
        return instance

    @property
    def events(self) -> list[DomainEvent]:
        """Uncommitted events pending persistence."""
        return self._changes

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        self._changes.clear()

    def assign_task(self, task_description: str) -> None:
        """Assign task, transitions to ANALYZING status."""
        task_event = TaskAssigned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
        )
        self._apply(task_event)
        self._changes.append(task_event)

    async def evaluate_complexity(self, llm_port: LLMPort, prompt_builder: PromptBuilder) -> None:
        """For PENDING agents: evaluate task complexity to become WORKER or MANAGER."""
        assert self.role == AgentRole.PENDING, f"Requires PENDING role, got {self.role}"
        assert self.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {self.status}"
        assert self.task_description, "Requires assigned task"

        prompt = prompt_builder.build_complexity_evaluation_prompt(
            task_description=self.task_description,
            agent_id=self.session_id,
            parent_task=None,
        )

        llm_config = ConfigResolver.resolve(self.config, operation="complexity_evaluation")
        response = await llm_port.query(prompt, llm_config.model_dump())

        try:
            clean_response = strip_markdown_code_block(response)
            data = json.loads(clean_response)
            complexity = data.get("complexity", "").lower()
            reasoning = data.get("reasoning", "")

            if complexity not in ("simple", "complex"):
                raise ValueError(f"Invalid complexity value: {complexity}")

            determined_role = AgentRole.WORKER if complexity == "simple" else AgentRole.MANAGER

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            failed_event = WorkFailed(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                reason=f"Failed to evaluate complexity: {e}",
            )
            self._apply(failed_event)
            self._changes.append(failed_event)
            return

        complexity_event = ComplexityEvaluated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            complexity=complexity,
            determined_role=determined_role.value,
            reasoning=reasoning,
        )
        self._apply(complexity_event)
        self._changes.append(complexity_event)

    async def evaluate_task(self, llm_port: LLMPort, prompt_builder: PromptBuilder) -> None:
        """For BOSS/MANAGER: decompose task into subtasks and spawn children."""
        assert self.role in (AgentRole.BOSS, AgentRole.MANAGER), (
            f"Requires BOSS/MANAGER, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {self.status}"

        if self.role == AgentRole.BOSS:
            prompt = prompt_builder.build_boss_delegation_prompt(
                task_description=self.task_description,
                agent_id=self.session_id,
                parent_task=None,
            )
        else:
            prompt = prompt_builder.build_manager_decomposition_prompt(
                task_description=self.task_description,
                agent_id=self.session_id,
                agent_role="MANAGER",
                parent_task=None,
            )

        llm_config = ConfigResolver.resolve(self.config, operation="task_decomposition")
        response = await llm_port.query(prompt, llm_config.model_dump())

        try:
            subtasks = SubtaskParser.parse_from_llm_response(response)
        except ValueError as e:
            failed_event = WorkFailed(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                reason=f"Failed to parse subtasks: {e}",
            )
            self._apply(failed_event)
            self._changes.append(failed_event)
            return

        subtasks_event = SubtasksDefined(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtasks=subtasks,
        )
        self._apply(subtasks_event)
        self._changes.append(subtasks_event)

        for subtask in subtasks:
            child_id = uuid4()
            child_event = ChildSpawned(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                child_id=child_id,
                child_role=AgentRole.PENDING.value,
                subtask=subtask,
                child_config=subtask.config,
            )
            self._apply(child_event)
            self._changes.append(child_event)

        status_event = StatusChanged(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            old_status=self.status.value,
            new_status=AgentStatus.WAITING.value,
            reason="Decomposed task, waiting for child agents",
        )
        self._apply(status_event)
        self._changes.append(status_event)

    async def execute_task(
        self,
        tool_port: WorkerToolPort,
        working_directory: str | None = None,
        workspace_context: str | None = None,
    ) -> None:
        """For WORKER: execute task using worker tool (Claude Code, OpenHands)."""
        assert self.role == AgentRole.WORKER, f"Requires WORKER, got {self.role}"
        assert self.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {self.status}"

        tool_name = self.config.tool

        started_event = CodeGenerationStarted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            tool_name=tool_name,
        )
        self._apply(started_event)
        self._changes.append(started_event)

        worker_system_prompt = (
            "<WORKER_INSTRUCTIONS>\n"
            "You are a WORKER agent with access to terminal and file editing tools.\n"
            "Your job is to EXECUTE the task by CREATING ACTUAL FILES in the workspace.\n\n"
            "IMPORTANT RULES:\n"
            "1. DO NOT just explain or provide code snippets - CREATE the actual files\n"
            "2. Use the file_editor tool to create/edit files in the workspace\n"
            "3. Use the terminal tool to run commands (e.g., to test your code)\n"
            "4. All files should be created in the current working directory\n"
            "5. After creating files, verify they exist by listing the directory\n"
            "</WORKER_INSTRUCTIONS>\n\n"
        )

        enhanced_description = f"{worker_system_prompt}<TASK>\n{self.task_description}\n</TASK>"

        if workspace_context:
            enhanced_description += (
                f"\n\n<WORKSPACE_CONTEXT>\n"
                f"You are working in a shared workspace. Other workers may have created files.\n"
                f"Current files in workspace:\n{workspace_context}\n"
                f"</WORKSPACE_CONTEXT>"
            )

        task_context: dict[str, Any] = {
            "session_id": self.session_id,
            "task_description": enhanced_description,
            "tool_name": tool_name,
            "config": self.config,
        }

        if working_directory:
            task_context["working_directory"] = working_directory

        try:
            async for tool_event in tool_port.run_session(task_context):
                event_data = tool_event.model_dump(exclude={"aggregate_id", "sequence_number"})
                event_data["aggregate_id"] = self.session_id
                event_data["sequence_number"] = self._next_sequence()
                corrected_event = type(tool_event)(**event_data)
                self._apply(corrected_event)
                self._changes.append(corrected_event)
        except ToolNotAvailableError as e:
            self.fail_with_reason(str(e))

    def handle_child_update(self, child_id: UUID, result: str) -> None:
        """For BOSS/MANAGER: record child completion, complete self when all children done."""
        assert self.role in (AgentRole.MANAGER, AgentRole.BOSS), (
            f"Requires BOSS/MANAGER, got {self.role}"
        )
        assert len(self.child_ids) > 0, "Requires agent with children"
        assert self.status == AgentStatus.WAITING, f"Requires WAITING status, got {self.status}"
        assert child_id in self.child_ids, f"child_id {child_id} not in spawned children"

        child_completed_event = ChildCompleted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            result=result,
        )
        self._apply(child_completed_event)
        self._changes.append(child_completed_event)

        if len(self.child_results) == len(self.child_ids):
            aggregated_result = self._aggregate_child_results()
            work_completed_event = WorkCompleted(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                result=aggregated_result,
            )
            self._apply(work_completed_event)
            self._changes.append(work_completed_event)

    def _aggregate_child_results(self) -> str:
        """Combine results from all completed children."""
        lines = ["All subtasks completed successfully:", ""]
        for child_id in self.child_ids:
            child_result = self.child_results.get(child_id, "No result")
            lines.append(f"- {child_result}")
        return "\n".join(lines)

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply event to update state. Raises TypeError for unregistered event types."""
        raise TypeError(f"No handler for {type(event).__name__}. Register with @_apply.register.")

    @_apply.register
    def _(self, event: AgentCreated) -> None:
        self.role = AgentRole(event.role)
        self.parent_id = event.parent_id
        adapter = TypeAdapter(AgentConfig)
        self.config = adapter.validate_python(event.config)
        self.status = AgentStatus.PENDING
        self.version += 1

    @_apply.register
    def _(self, event: TaskAssigned) -> None:
        self.task_description = event.task_description
        self.status = AgentStatus.ANALYZING
        self.version += 1

    @_apply.register
    def _(self, event: StatusChanged) -> None:
        self.status = AgentStatus(event.new_status)
        self.version += 1

    @_apply.register
    def _(self, event: SubtasksDefined) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: ChildSpawned) -> None:
        assert event.child_role != AgentRole.BOSS.value, "Cannot spawn BOSS child"
        self.child_ids.append(event.child_id)
        self.version += 1

    @_apply.register
    def _(self, event: WorkFailed) -> None:
        self.status = AgentStatus.FAILED
        self.error_message = event.reason
        self.version += 1

    @_apply.register
    def _(self, event: CodeGenerationStarted) -> None:
        self.status = AgentStatus.IN_PROGRESS
        self.version += 1

    @_apply.register
    def _(self, event: ThoughtCaptured) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: WorkCompleted) -> None:
        self.status = AgentStatus.COMPLETED
        self.result = event.result
        self.version += 1

    @_apply.register
    def _(self, event: ChildCompleted) -> None:
        self.child_results[event.child_id] = event.result
        self.version += 1

    @_apply.register
    def _(self, event: ComplexityEvaluated) -> None:
        self.role = AgentRole(event.determined_role)
        self.version += 1

    def _initialize_defaults(self, session_id: UUID) -> None:
        self.session_id: UUID = session_id
        self.role: AgentRole = AgentRole.BOSS
        self.status: AgentStatus = AgentStatus.PENDING
        self.parent_id: UUID | None = None
        self.child_ids: list[UUID] = []
        self.child_results: dict[UUID, str] = {}
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.config: AgentConfig
        self.version: int = 0
        self._changes: list[DomainEvent] = []
        self._sequence: int = 0

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "AgentSession":
        """Reconstruct AgentSession by replaying events."""
        assert events, "Cannot load from empty event history"
        first_event = events[0]
        assert isinstance(first_event, AgentCreated), (
            f"First event must be AgentCreated, got {type(first_event).__name__}"
        )

        instance = cls(first_event.aggregate_id)
        for event in events:
            instance._apply(event)
            instance._sequence = max(instance._sequence, event.sequence_number)
        return instance

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def is_terminal(self) -> bool:
        """True if COMPLETED or FAILED."""
        return self.status in (AgentStatus.COMPLETED, AgentStatus.FAILED)

    def is_leaf(self) -> bool:
        """True if WORKER with no children."""
        return self.role == AgentRole.WORKER and len(self.child_ids) == 0

    def fail_with_reason(self, reason: str) -> None:
        """Mark agent as failed with WorkFailed event."""
        failed_event = WorkFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            reason=reason,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)
