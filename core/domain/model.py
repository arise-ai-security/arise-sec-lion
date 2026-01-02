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
    AgentTerminated,
    AllChildrenFailed,
    AllSubordinatesFailed,
    BudgetAdjusted,
    BudgetAllocated,
    BudgetRecollected,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    ContextInherited,
    ContextPublished,
    DomainEvent,
    FirstSuccessRecorded,
    SourceContextExtracted,
    StatusChanged,
    SubordinatesSpawned,
    SubtaskRetried,
    SubtasksDefined,
    SubtreeAborted,
    TaskAssigned,
    TaskDequeued,
    TaskEnqueued,
    TaskReinjected,
    TerminationReason,
    ThoughtCaptured,
    VerificationCompleted,
    VerificationHeuristicEvaluated,
    VerificationInjected,
    VerifierSpawned,
    WorkCompleted,
    WorkFailed,
)
from core.domain.exceptions import ToolNotAvailableError
from core.domain.execution_context import ExecutionContext
from core.domain.llm_response import LLMResponse
from core.domain.prompt_builder import PromptBuilder, is_security_task
from core.domain.services import SubtaskParser, strip_markdown_code_block
from core.domain.subtask import Subtask, SubtaskJustification, WorkerReport
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort


class AgentRole(str, Enum):
    """Agent role: BOSS (root), PENDING (awaiting eval), MANAGER (decomposes), WORKER (executes)."""

    BOSS = "boss"
    PENDING = "pending"
    MANAGER = "manager"
    WORKER = "worker"


class AgentStatus(str, Enum):
    """Current execution status of an agent session.

    Attributes:
        PENDING: Agent has been created but not yet started.
        ANALYZING: Agent is analyzing the task (planning, decomposing).
        IN_PROGRESS: Agent is actively working on its task.
        WAITING: Agent is waiting for child agents to complete.
        COMPLETED: Agent has successfully completed its task.
        FAILED: Agent encountered an error and cannot proceed.
        BLOCKED: Agent is blocked waiting for external input or resolution.
    """

    PENDING = "pending"
    ANALYZING = "analyzing"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    TERMINATED = "terminated"
    VERIFYING = "verifying"


class AgentSession:
    """Event-sourced aggregate representing an agent session.

    AgentSession is the core aggregate in our event-sourced system. Its state
    is derived by replaying domain events. All state changes occur by applying
    events, never by direct mutation.

    Event Sourcing Pattern:
        - All state changes create events (stored in self._changes)
        - Events are applied via _apply() method
        - State can be reconstructed by replaying events via load_from_history()

    Attributes:
        session_id: Unique identifier for this agent session.
        role: The hierarchical role (BOSS, MANAGER, or WORKER).
        status: Current execution status of the agent.
        parent_id: ID of the parent agent session (None for BOSS).
        child_ids: List of child agent session IDs spawned by this agent.
        task_description: The task assigned to this agent.
        result: The final result/output when status is COMPLETED.
        error_message: Error details when status is FAILED.
        config: Configuration for this agent.
        version: Event stream version (for Optimistic Concurrency Control).
    """

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
        tree_sequence_id: int = 0,
    ) -> "AgentSession":
        instance = cls(session_id)
        event = AgentCreated(
            aggregate_id=session_id,
            sequence_number=instance._next_sequence(),
            role=role.value,
            parent_id=parent_id,
            config=config,
            tree_sequence_id=tree_sequence_id,
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

    def assign_task(
        self,
        task_description: str,
        justification: SubtaskJustification | None = None,
    ) -> None:
        """Assign task with optional supervisor justification, transitions to ANALYZING status."""
        task_event = TaskAssigned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
            justification=justification,
        )
        self._apply(task_event)
        self._changes.append(task_event)

    def shortcut_to_worker(self, reason: str = "randomly selected to skip complexity evaluation") -> None:
        """Bypass complexity evaluation and directly become WORKER."""
        assert self.role == AgentRole.PENDING, f"Requires PENDING role, got {self.role}"
        assert self.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {self.status}"

        complexity_event = ComplexityEvaluated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
            reasoning=f"Shortcut: {reason}",
        )
        self._apply(complexity_event)
        self._changes.append(complexity_event)

    async def evaluate_complexity(self, llm_port: LLMPort, prompt_builder: PromptBuilder) -> None:
        """For PENDING agents: evaluate task complexity to become WORKER or MANAGER.

        Automatically detects security tasks and provides security-specific
        complexity evaluation guidance.
        """
        assert self.role == AgentRole.PENDING, f"Requires PENDING role, got {self.role}"
        assert self.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {self.status}"
        assert self.task_description, "Requires assigned task"

        # Build base complexity evaluation prompt
        prompt = prompt_builder.build_complexity_evaluation_prompt(
            task_description=self.task_description,
            agent_id=self.session_id,
            parent_task=None,
            justification=self.supervisor_justification,
        )

        # Append security-specific guidance for security tasks
        if is_security_task(self.task_description):
            try:
                security_guidance = prompt_builder.env.get_template(
                    "security/complexity_security.j2"
                ).render()
                prompt = f"{prompt}\n\n{security_guidance}"
            except Exception:
                # Fall back to base prompt if security template missing
                pass

        llm_config = ConfigResolver.resolve(self.config, operation="complexity_evaluation")

        try:
            response = await llm_port.query(prompt, llm_config.model_dump())
        except Exception as e:
            failed_event = WorkFailed(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                reason=f"LLM query failed during complexity evaluation: {e}",
            )
            self._apply(failed_event)
            self._changes.append(failed_event)
            return

        try:
            clean_response = strip_markdown_code_block(response)
            if not clean_response.strip():
                raise ValueError("Empty response from LLM")
            data = json.loads(clean_response)
            complexity = data.get("complexity", "").lower()
            reasoning = data.get("reasoning", "")

            if complexity not in ("simple", "complex"):
                raise ValueError(f"Invalid complexity value: '{complexity}'. Expected 'simple' or 'complex'")

            determined_role = AgentRole.WORKER if complexity == "simple" else AgentRole.MANAGER

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            failed_event = WorkFailed(
                aggregate_id=self.session_id,
                sequence_number=self._next_sequence(),
                reason=f"Failed to evaluate complexity: {e}. Response was: {response[:200] if response else 'empty'}",
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
        """For BOSS/MANAGER: decompose task into subtasks and spawn children.

        Respects execution context limits:
        - max_depth: Forces WORKER role for children at max depth
        - max_children_per_node: Limits number of subtasks spawned

        Automatically detects security tasks and uses appropriate prompts.
        """
        assert self.role in (AgentRole.BOSS, AgentRole.MANAGER), (
            f"Requires BOSS/MANAGER, got {self.role}"
        )
        assert self.status == AgentStatus.ANALYZING, f"Requires ANALYZING status, got {self.status}"

        # Use auto_prompt for automatic security task detection
        prompt = prompt_builder.build_auto_prompt(
            task_description=self.task_description,
            agent_id=self.session_id,
            agent_role=self.role.value.upper(),
            parent_task=None,
            justification=self.supervisor_justification,
        )

        llm_config = ConfigResolver.resolve(self.config, operation="task_decomposition")
        response = await llm_port.query(prompt, llm_config.model_dump())

        try:
            # Pass parent's tool as default so children inherit it
            subtasks = SubtaskParser.parse_from_llm_response(
                response, default_tool=self.config.tool
            )
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
        cross_session_context: str | None = None,
    ) -> None:
        """For WORKER: execute task using worker tool (Claude Code, OpenHands).

        Args:
            tool_port: Port for worker tool execution.
            working_directory: Working directory for the tool.
            workspace_context: File listing from shared workspace.
            cross_session_context: Relevant context from previous sessions.
        """
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

        supervisor_context = ""
        if self.supervisor_justification and self.supervisor_justification.has_content():
            j = self.supervisor_justification
            supervisor_context = (
                "<SUPERVISOR_EXPECTATIONS>\n"
                "Your supervisor assigned this task with the following context and expectations:\n\n"
                f"**Supervisor's Original Task**: {j.parent_task}\n\n"
                f"**Objective for This Subtask**: {j.objective}\n\n"
                f"**Why This Was Assigned to You**: {j.split_reason}\n\n"
                f"**Suggested Approach**: {j.plan}\n\n"
                f"**Why This Should Work**: {j.why_it_may_work}\n\n"
                f"**Expected Deliverables**: {j.expected_results}\n"
            )
            # Add budget allocation context if available
            if j.budget_allocation:
                supervisor_context += (
                    "\n## Budget Allocation Context\n"
                    "Your supervisor has allocated resources for this task with the following reasoning:\n\n"
                    f"**Budget Allocation**: {j.budget_allocation}\n\n"
                    f"**Complexity Assessment**: {j.complexity_assessment}\n\n"
                    f"**Significance/Priority**: {j.significance_weight}\n\n"
                    f"**Resource Justification**: {j.resource_justification}\n\n"
                    "Use this context to calibrate your effort:\n"
                    "- Higher budget % indicates more thorough work expected\n"
                    "- The complexity assessment tells you expected difficulty\n"
                    "- Significance helps prioritize quality vs. speed\n"
                )
            supervisor_context += "</SUPERVISOR_EXPECTATIONS>\n\n"

        enhanced_description = f"{worker_system_prompt}{supervisor_context}<TASK>\n{self.task_description}\n</TASK>"

        # Add cross-session context if available (from context dashboard)
        if cross_session_context:
            enhanced_description += f"\n\n{cross_session_context}"

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

        # Collect captured thoughts for generating worker report
        captured_thoughts: list[str] = []

        try:
            async for tool_event in tool_port.run_session(task_context):
                event_data = tool_event.model_dump(exclude={"aggregate_id", "sequence_number"})
                event_data["aggregate_id"] = self.session_id
                event_data["sequence_number"] = self._next_sequence()

                # Collect thought content for report generation
                if isinstance(tool_event, ThoughtCaptured):
                    captured_thoughts.append(tool_event.content)

                # If WorkCompleted, enrich with worker report
                if isinstance(tool_event, WorkCompleted):
                    worker_report = self._generate_worker_report(
                        event_data.get("result", ""),
                        captured_thoughts,
                    )
                    event_data["worker_report"] = worker_report

                corrected_event = type(tool_event)(**event_data)
                self._apply(corrected_event)
                self._changes.append(corrected_event)
        except ToolNotAvailableError as e:
            self.fail_with_reason(str(e))

    def _generate_worker_report(
        self,
        result: str,
        captured_thoughts: list[str],
    ) -> WorkerReport:
        """Generate a worker report based on task context and captured output.

        This synthesizes the worker's justification for their work, including:
        - The original task that was assigned
        - How they approached it (from captured thoughts)
        - Key observations and discoveries during execution
        - What was delivered (specific file names worked on)
        - Evidence showing how supervisor expectations were fulfilled
        """
        import re

        # Extract approach, observations, and challenges from captured thoughts
        approach_lines = []
        challenges_lines = []
        observation_lines = []
        files_worked_on: set[str] = set()
        technical_discoveries: list[str] = []
        # Track specific changes per file: {file_path: [list of changes/operations]}
        file_changes: dict[str, list[str]] = {}

        # Patterns to extract file names with their operations from progress messages
        file_patterns = [
            r"(?:created|wrote|modified|updated|edited|generated|deleted|added)\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)",
            r"(?:creating|writing|modifying|updating|editing|generating|deleting|adding)\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)",
            r"(?:file|File)\s*[:\s]+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)",
            r"([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)(?:\s+(?:created|modified|updated|written))",
        ]

        # Patterns to extract file + operation context (what was done to the file)
        file_operation_patterns = [
            # "created/wrote file.py with/containing X" or "created file.py: X"
            (r"(created|wrote|generated)\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)['\"]?\s*(?:with|containing|:)\s*(.+?)(?:\.|$)", "created"),
            # "added X to file.py" or "added X in file.py"
            (r"added\s+(.+?)\s+(?:to|in)\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)", "added"),
            # "updated/modified file.py to X" or "updated file.py with X"
            (r"(updated|modified)\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)['\"]?\s*(?:to|with|:)\s*(.+?)(?:\.|$)", "updated"),
            # "implemented X in file.py"
            (r"implemented\s+(.+?)\s+in\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)", "implemented"),
            # "fixed X in file.py"
            (r"fixed\s+(.+?)\s+in\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)", "fixed"),
            # "refactored X in file.py"
            (r"refactored\s+(.+?)\s+in\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)", "refactored"),
            # "removed X from file.py"
            (r"removed\s+(.+?)\s+from\s+['\"]?([^'\"<>\n]+\.[a-zA-Z0-9]+)", "removed"),
        ]

        # Keywords for categorizing thoughts
        action_keywords = ["creating", "writing", "running", "executing", "installing", "adding", "updating"]
        challenge_keywords = ["error", "failed", "issue", "problem", "fix", "warning", "retry"]
        discovery_keywords = ["found", "discovered", "noticed", "realized", "learned", "observed", "detected", "identified"]
        result_keywords = ["completed", "success", "done", "finished", "passed", "works", "verified", "tested"]
        # Worker's own reasoning keywords - why they chose an approach or believe it works
        reasoning_keywords = [
            "because", "this works", "the reason", "this should", "this will",
            "decided to", "chose to", "better to", "makes sense", "ensures",
            "allows", "enables", "prevents", "avoids", "solves", "addresses",
            "correct", "proper", "appropriate", "necessary", "required",
        ]

        worker_reasoning_lines: list[str] = []

        for thought in captured_thoughts:
            thought_lower = thought.lower()
            thought_clean = thought.strip()

            # Categorize the thought
            if any(kw in thought_lower for kw in challenge_keywords):
                challenges_lines.append(thought_clean)
            elif any(kw in thought_lower for kw in action_keywords):
                approach_lines.append(thought_clean)

            # Extract observations: discoveries, learnings, and results
            if any(kw in thought_lower for kw in discovery_keywords):
                technical_discoveries.append(thought_clean)
            if any(kw in thought_lower for kw in result_keywords):
                observation_lines.append(thought_clean)

            # Extract worker's own reasoning about why their approach is justified
            if any(kw in thought_lower for kw in reasoning_keywords):
                worker_reasoning_lines.append(thought_clean)

            # Extract file names from progress messages
            for pattern in file_patterns:
                matches = re.findall(pattern, thought, re.IGNORECASE)
                for match in matches:
                    file_path = match.strip().strip("'\"")
                    if file_path and not file_path.startswith(("http://", "https://", "/")):
                        files_worked_on.add(file_path)

            # Extract file + operation context (what specific changes were made)
            for pattern, op_type in file_operation_patterns:
                matches = re.findall(pattern, thought, re.IGNORECASE)
                for match in matches:
                    if op_type == "added":
                        # Pattern: added X to/in file.py -> (X, file.py)
                        change_desc, file_path = match[0], match[1]
                        change = f"added {change_desc.strip()[:50]}"
                    elif op_type in ("created", "updated"):
                        # Pattern: created/updated file.py with X -> (verb, file.py, X)
                        file_path, change_desc = match[1], match[2]
                        change = f"{op_type} with {change_desc.strip()[:50]}"
                    elif op_type in ("implemented", "fixed", "refactored", "removed"):
                        # Pattern: verb X in/from file.py -> (X, file.py)
                        change_desc, file_path = match[0], match[1]
                        change = f"{op_type} {change_desc.strip()[:50]}"
                    else:
                        continue

                    file_path = file_path.strip().strip("'\"")
                    if file_path and not file_path.startswith(("http://", "https://", "/")):
                        files_worked_on.add(file_path)
                        if file_path not in file_changes:
                            file_changes[file_path] = []
                        if change not in file_changes[file_path]:
                            file_changes[file_path].append(change)

        # Also extract from result
        for pattern in file_patterns:
            matches = re.findall(pattern, result, re.IGNORECASE)
            for match in matches:
                file_path = match.strip().strip("'\"")
                if file_path and not file_path.startswith(("http://", "https://")):
                    files_worked_on.add(file_path)

        # Build approach from actual actions taken
        approach = (
            "; ".join(approach_lines[:5]) if approach_lines
            else "Executed task using available tools"
        )

        # Build challenges from actual issues encountered
        challenges = (
            "; ".join(challenges_lines[:3]) if challenges_lines
            else "No significant challenges encountered"
        )

        # Build observations from discoveries and results (NEW)
        all_observations = technical_discoveries[:3] + observation_lines[:3]
        if all_observations:
            observations = "; ".join(all_observations[:5])
        else:
            # Extract meaningful observations from result if no discoveries captured
            result_lines = [line.strip() for line in result.split("\n") if line.strip()][:3]
            observations = "; ".join(result_lines) if result_lines else "Task executed as planned"

        # Build reasoning from WORKER'S OWN THOUGHTS about why their work is justified
        reasoning_parts = []

        # Primary: Worker's own reasoning about why their approach works
        if worker_reasoning_lines:
            # Use up to 3 of the worker's reasoning thoughts
            for line in worker_reasoning_lines[:3]:
                reasoning_parts.append(line[:150])
        else:
            # Fallback: Construct reasoning from what was done and discovered
            if technical_discoveries:
                reasoning_parts.append(f"Discovered: {technical_discoveries[0][:100]}")
            if approach_lines:
                reasoning_parts.append(f"Approach taken: {approach_lines[0][:100]}")
            if observation_lines:
                reasoning_parts.append(f"Verified: {observation_lines[0][:100]}")

        # Add concrete evidence of why the work is valid
        if files_worked_on:
            reasoning_parts.append(f"Produced {len(files_worked_on)} deliverable(s)")
        if observation_lines and "success" in " ".join(observation_lines).lower():
            reasoning_parts.append("Execution verified successful")

        reasoning = (
            "; ".join(reasoning_parts) if reasoning_parts
            else "Task executed using standard approach with successful completion"
        )

        # Build deliverables with specific file changes (not just file names)
        if files_worked_on:
            sorted_files = sorted(files_worked_on)
            deliverable_parts = []

            for file_path in sorted_files:
                if file_path in file_changes and file_changes[file_path]:
                    # Include specific changes for this file
                    changes = file_changes[file_path][:2]  # Limit to 2 changes per file
                    change_str = ", ".join(changes)
                    deliverable_parts.append(f"{file_path}: {change_str}")
                else:
                    # Fall back to just the file name if no specific changes captured
                    deliverable_parts.append(file_path)

            # Format as detailed deliverables
            if any(file_changes.values()):
                deliverables = "Changes made: " + "; ".join(deliverable_parts[:8])
            else:
                deliverables = f"Files modified: {', '.join(sorted_files[:8])}"

            # Add count if there are more files
            if len(sorted_files) > 8:
                deliverables += f" (+{len(sorted_files) - 8} more files)"
        else:
            result_lines = result.split("\n") if result else []
            deliverables = (
                "; ".join(line.strip() for line in result_lines[:5] if line.strip())
                if result_lines else "Task completed"
            )

        # Build fulfillment evidence linking results to supervisor expectations (NEW)
        fulfillment_parts = []
        if self.supervisor_justification and self.supervisor_justification.has_content():
            j = self.supervisor_justification

            # Check objective fulfillment
            if j.objective and j.objective != "(legacy event)":
                fulfillment_parts.append(f"Objective '{j.objective[:50]}...' addressed")

            # Check expected results
            if j.expected_results and j.expected_results != "(legacy event)":
                if files_worked_on:
                    fulfillment_parts.append(
                        f"Expected deliverables achieved: {len(files_worked_on)} file(s) produced"
                    )
                if not challenges_lines or "error" not in " ".join(challenges_lines).lower():
                    fulfillment_parts.append("Task completed without critical errors")

            # Link plan to actual approach
            if j.plan and j.plan != "(legacy event)" and approach_lines:
                fulfillment_parts.append(
                    f"Supervisor's plan executed via: {approach_lines[0][:80]}"
                )

        if not fulfillment_parts:
            fulfillment_parts.append("Task completed successfully per assignment")

        fulfillment_evidence = "; ".join(fulfillment_parts[:4])

        return WorkerReport(
            original_task=self.task_description or "",
            approach=approach[:1500],  # Truncate to reasonable length
            reasoning=reasoning[:1500],
            deliverables=deliverables[:1500],
            challenges=challenges[:1500],
            observations=observations[:2000],  # Key observations need more space
            fulfillment_evidence=fulfillment_evidence[:2000],  # Evidence needs more space
        )

    def handle_child_update(
        self,
        child_id: UUID,
        result: str,
        worker_report: WorkerReport | None = None,
    ) -> None:
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
            worker_report=worker_report,
        )
        self._apply(child_completed_event)
        self._changes.append(child_completed_event)

        # Check if all children are accounted for (completed + failed)
        total_done = len(self.child_results) + len(self.child_failures)
        if total_done == len(self.child_ids):
            if len(self.child_failures) > 0:
                # Some children failed, parent fails
                failed_reasons = [f"{cid}: {r}" for cid, r in self.child_failures.items()]
                self.fail_with_reason(f"Some children failed: {'; '.join(failed_reasons)}")
            else:
                # All succeeded
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

    def record_context_published(
        self,
        entry_id: UUID,
        work_title: str,
        worker_id: UUID,
        objective: str = "",
        justification_summary: str = "",
    ) -> None:
        """Record that context was published for a completed worker.

        This creates an audit trail event for context sharing. The actual
        context entry is stored in the context dashboard (external store).

        Args:
            entry_id: UUID of the context entry in the dashboard.
            work_title: Descriptive title for the work.
            worker_id: UUID of the worker that completed the task.
            objective: What the subtask aimed to achieve.
            justification_summary: Summary of why task was assigned.
        """
        event = ContextPublished(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            work_title=work_title,
            context_entry_id=entry_id,
            worker_id=worker_id,
            objective=objective,
            justification_summary=justification_summary,
        )
        self._apply(event)
        self._changes.append(event)

    def record_context_inherited(
        self,
        entry_ids: list[UUID],
        total_available: int,
    ) -> None:
        """Record that context was inherited from the global dashboard.

        This creates an audit trail event for cross-session learning.
        The worker received relevant context from previous sessions.

        Args:
            entry_ids: List of context entry UUIDs that were inherited.
            total_available: Total number of entries available in the dashboard.
        """
        event = ContextInherited(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            entry_ids=entry_ids,
            total_available=total_available,
        )
        self._apply(event)
        self._changes.append(event)

    def record_source_context_extracted(
        self,
        context_entry_id: UUID,
        extraction_summary: str,
        key_references: list[str] | None = None,
        has_bug_report: bool = False,
        has_error_details: bool = False,
        has_file_references: bool = False,
        inferred_cwes: list[str] | None = None,
        cwe_reasoning: dict[str, str] | None = None,
        recommended_sanitizers: list[str] | None = None,
        fix_patterns: dict[str, str] | None = None,
    ) -> None:
        """Record that source context was extracted from the Boss prompt.

        This creates an audit trail event for source context extraction.
        The Boss extracted key information from the original user prompt
        and published it to the context dashboard for workers to reference.

        Args:
            context_entry_id: UUID of the context entry in the dashboard.
            extraction_summary: Brief summary of what was extracted.
            key_references: List of key references (files, commits, etc.).
            has_bug_report: Whether the prompt contained bug report details.
            has_error_details: Whether the prompt contained error messages.
            has_file_references: Whether the prompt contained file paths.
            inferred_cwes: Inferred CWE IDs from bug report analysis.
            cwe_reasoning: Reasoning for each inferred CWE.
            recommended_sanitizers: Recommended sanitizers based on CWE types.
            fix_patterns: Recommended fix patterns per CWE.
        """
        event = SourceContextExtracted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            context_entry_id=context_entry_id,
            extraction_summary=extraction_summary,
            key_references=key_references or [],
            has_bug_report=has_bug_report,
            has_error_details=has_error_details,
            has_file_references=has_file_references,
            inferred_cwes=inferred_cwes or [],
            cwe_reasoning=cwe_reasoning or {},
            recommended_sanitizers=recommended_sanitizers or [],
            fix_patterns=fix_patterns or {},
        )
        self._apply(event)
        self._changes.append(event)

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
        self.tree_sequence_id = event.tree_sequence_id
        self.version += 1

    @_apply.register
    def _(self, event: TaskAssigned) -> None:
        self.task_description = event.task_description
        self.status = AgentStatus.ANALYZING
        self.supervisor_justification = event.justification
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
        self.worker_report = event.worker_report
        self.version += 1

    @_apply.register
    def _(self, event: ChildCompleted) -> None:
        self.child_results[event.child_id] = event.result
        if event.worker_report is not None:
            self.child_worker_reports[event.child_id] = event.worker_report
        self.version += 1

    @_apply.register
    def _(self, event: ComplexityEvaluated) -> None:
        self.role = AgentRole(event.determined_role)
        self.version += 1

    @_apply.register
    def _(self, event: BudgetAllocated) -> None:
        """Apply BudgetAllocated event to set initial budget.

        Args:
            event: BudgetAllocated event with amount and source.
        """
        self.current_budget = event.amount
        self.version += 1

    @_apply.register
    def _(self, event: BudgetAdjusted) -> None:
        """Apply BudgetAdjusted event to update budget balance.

        Args:
            event: BudgetAdjusted event with adjustment and new balance.
        """
        self.current_budget = event.new_balance
        self.version += 1

    @_apply.register
    def _(self, event: TaskEnqueued) -> None:
        """Apply TaskEnqueued event to add subtask to queue.

        Args:
            event: TaskEnqueued event with subtask.
        """
        self.task_queue.append(event.subtask)
        self.version += 1

    @_apply.register
    def _(self, event: TaskDequeued) -> None:
        """Apply TaskDequeued event to remove subtask from queue.

        Args:
            event: TaskDequeued event with subtask that was removed.
        """
        # Remove the first occurrence of the subtask from queue
        if event.subtask in self.task_queue:
            self.task_queue.remove(event.subtask)
        self.version += 1

    # ========================================================================
    # Termination Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: AgentTerminated) -> None:
        """Apply AgentTerminated event to mark agent as terminated.

        Args:
            event: AgentTerminated event with termination reason.
        """
        self.status = AgentStatus.TERMINATED
        self.termination_reason = event.reason
        self.version += 1

    @_apply.register
    def _(self, event: SubtreeAborted) -> None:
        """Apply SubtreeAborted event (audit trail for subtree abortion).

        Args:
            event: SubtreeAborted event with affected child IDs.
        """
        # This event is primarily for audit trail
        # Actual child termination is handled by execution service
        self.version += 1

    # ========================================================================
    # Budget Recollection Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: BudgetRecollected) -> None:
        """Apply BudgetRecollected event to update budget after child completion.

        Args:
            event: BudgetRecollected event with recollection details.
        """
        self.current_budget += event.amount_recollected
        self.version += 1

    @_apply.register
    def _(self, event: ChildFailed) -> None:
        """Apply ChildFailed event to track child failure.

        Args:
            event: ChildFailed event with failure details.
        """
        self.child_failures[event.child_id] = event.failure_reason
        self.version += 1

    @_apply.register
    def _(self, event: AllChildrenFailed) -> None:
        """Apply AllChildrenFailed event (audit trail for total failure).

        Args:
            event: AllChildrenFailed event with penalty information.
        """
        # This event is for audit trail and analytics
        self.version += 1

    # ========================================================================
    # Verification Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: VerificationInjected) -> None:
        """Apply VerificationInjected event to track pending verification.

        Args:
            event: VerificationInjected event with verification target.
        """
        self.verification_pending.append(event.target_child_id)
        self.status = AgentStatus.VERIFYING
        self.version += 1

    @_apply.register
    def _(self, event: VerifierSpawned) -> None:
        """Apply VerifierSpawned event to track verifier agent.

        Args:
            event: VerifierSpawned event with verifier details.
        """
        self.child_ids.append(event.verifier_id)
        self.version += 1

    @_apply.register
    def _(self, event: VerificationCompleted) -> None:
        """Apply VerificationCompleted event to process verification result.

        Args:
            event: VerificationCompleted event with verification outcome.
        """
        if event.target_child_id in self.verification_pending:
            self.verification_pending.remove(event.target_child_id)
        import time
        self.last_verification_time = time.time()
        self.version += 1

    @_apply.register
    def _(self, event: TaskReinjected) -> None:
        """Apply TaskReinjected event to add task back to front of queue.

        Args:
            event: TaskReinjected event with task to redo.
        """
        # Insert at the front of the queue (priority redo)
        self.task_queue.insert(0, event.subtask)
        self.version += 1

    @_apply.register
    def _(self, event: VerificationHeuristicEvaluated) -> None:
        """Apply VerificationHeuristicEvaluated event (audit trail only).

        Args:
            event: VerificationHeuristicEvaluated event with decision details.
        """
        # This event is for audit trail and tuning heuristics
        self.version += 1

    # ========================================================================
    # Multi-Model Strategy Event Handlers
    # ========================================================================

    @_apply.register
    def _(self, event: SubordinatesSpawned) -> None:
        """Apply SubordinatesSpawned event to track parallel subordinates.

        Args:
            event: SubordinatesSpawned event with subordinate configurations.
        """
        # Track current subtask and its subordinates
        self.current_subtask = event.subtask
        self.current_subtask_subordinates = []

        for config in event.subordinate_configs:
            if "child_id" in config:
                child_id = config["child_id"]
                if isinstance(child_id, str):
                    child_id = UUID(child_id)
                self.child_ids.append(child_id)
                self.current_subtask_subordinates.append(child_id)
                if "budget" in config:
                    self.child_budgets[child_id] = config["budget"]
        self.version += 1

    @_apply.register
    def _(self, event: FirstSuccessRecorded) -> None:
        """Apply FirstSuccessRecorded event when first parallel child succeeds.

        Args:
            event: FirstSuccessRecorded event with winning child details.
        """
        # Record the winning result
        self.child_results[event.winning_child_id] = event.result or event.method_used
        # Update budget with recollected amounts
        self.current_budget += event.budget_recollected_from_winner
        self.current_budget += event.budget_recollected_from_siblings
        self.version += 1

    @_apply.register
    def _(self, event: AllSubordinatesFailed) -> None:
        """Apply AllSubordinatesFailed event when all parallel children fail.

        Args:
            event: AllSubordinatesFailed event with failure details.
        """
        # Record all failures
        for child_id in event.failed_child_ids:
            reason = event.failure_reasons.get(str(child_id), "Unknown failure")
            self.child_failures[child_id] = reason
        self.version += 1

    @_apply.register
    def _(self, event: SubtaskRetried) -> None:
        """Apply SubtaskRetried event to re-insert revised subtask to queue.

        Args:
            event: SubtaskRetried event with revised subtask.
        """
        # Insert revised subtask at the head of the queue
        self.task_queue.insert(0, event.revised_subtask)
        self.version += 1

    @_apply.register
    def _(self, event: ContextPublished) -> None:
        """Apply ContextPublished event (audit trail only, no state change)."""
        self.version += 1

    @_apply.register
    def _(self, event: ContextInherited) -> None:
        """Apply ContextInherited event (audit trail only, no state change)."""
        self.version += 1

    @_apply.register
    def _(self, event: SourceContextExtracted) -> None:
        """Apply SourceContextExtracted event (audit trail only, no state change)."""
        self.version += 1

    def _initialize_defaults(self, session_id: UUID) -> None:
        self.session_id: UUID = session_id
        self.role: AgentRole = AgentRole.BOSS
        self.status: AgentStatus = AgentStatus.PENDING
        self.parent_id: UUID | None = None
        self.child_ids: list[UUID] = []
        self.child_results: dict[UUID, str] = {}  # Track completed children
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.config: AgentConfig  # Type-safe config (deserialized from events)
        self.version: int = 0
        self._changes: list[DomainEvent] = []
        self._sequence: int = 0

        # Tree sequence ID for left-to-right worker execution ordering
        # Workers with lower sequence IDs must complete before higher ones execute
        self.tree_sequence_id: int = 0

        # Budget tracking (reward/penalty mechanism)
        self.current_budget: float = 0.0
        self.child_budgets: dict[UUID, float] = {}  # Track allocated budget per child
        self.child_failures: dict[UUID, str] = {}  # Track failed children with reasons

        # Task queue for subtask management
        self.task_queue: list[Subtask] = []
        self.subtask_retry_counts: dict[str, int] = {}  # Track retries per subtask description

        # Termination tracking
        self.termination_reason: str | None = None

        # Verification tracking
        self.verification_pending: list[UUID] = []  # Child IDs pending verification
        self.last_verification_time: float = 0.0

        # Multi-model strategy tracking
        self.current_subtask: Subtask | None = None
        self.current_subtask_subordinates: list[UUID] = []

        # Supervisor's justification for this agent's assigned task
        self.supervisor_justification: SubtaskJustification | None = None

        # Worker's report upon completion (for WORKER agents)
        self.worker_report: WorkerReport | None = None

        # Child worker reports (for tracking reports from children)
        self.child_worker_reports: dict[UUID, WorkerReport] = {}

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
        """Check if the agent is in a terminal state.

        Returns:
            True if status is COMPLETED or FAILED, False otherwise.
        """
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

    def allocate_budget(self, amount: float, source: str = "initial") -> None:
        """Allocate budget to this agent.

        Budget represents the numeric resource allocation that an agent can use
        to perform its tasks. This method is typically called when creating a
        new agent or when a parent allocates resources to a child.

        Args:
            amount: The budget amount to allocate (must be positive).
            source: Source of the budget allocation (e.g., "initial", "parent", "reward").

        Raises:
            ValueError: If amount is negative.
        """
        if amount < 0:
            raise ValueError(f"Budget amount must be non-negative, got {amount}")

        budget_event = BudgetAllocated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            amount=amount,
            source=source,
        )
        self._apply(budget_event)
        self._changes.append(budget_event)

    def adjust_budget(self, adjustment: float, reason: str) -> None:
        """Adjust the agent's budget by a positive or negative amount.

        This method is used by the reward mechanism to increase budget when
        subordinates succeed or decrease budget when subordinates fail.

        Args:
            adjustment: Amount to adjust (positive for increase, negative for decrease).
            reason: Explanation for the budget adjustment.

        Note:
            Budget can go negative if penalties exceed current budget.
            This is allowed to track "debt" or over-allocation scenarios.
        """
        new_balance = self.current_budget + adjustment

        adjustment_event = BudgetAdjusted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            adjustment=adjustment,
            reason=reason,
            new_balance=new_balance,
        )
        self._apply(adjustment_event)
        self._changes.append(adjustment_event)

    def enqueue_task(self, subtask: Subtask) -> None:
        """Add a subtask to the end of the task queue.

        The task queue is a FIFO queue where agents store subtasks that need
        to be processed. Tasks are dequeued in order for execution.

        Args:
            subtask: The Subtask to add to the queue.
        """
        enqueue_event = TaskEnqueued(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
        )
        self._apply(enqueue_event)
        self._changes.append(enqueue_event)

    def dequeue_task(self) -> Subtask | None:
        """Remove and return the next task from the task queue.

        Returns:
            The next Subtask in the queue, or None if queue is empty.
        """
        if not self.task_queue:
            return None

        subtask = self.task_queue[0]
        dequeue_event = TaskDequeued(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
        )
        self._apply(dequeue_event)
        self._changes.append(dequeue_event)

        return subtask

    def peek_next_task(self) -> Subtask | None:
        """Look at the next task in the queue without removing it.

        Returns:
            The next Subtask in the queue, or None if queue is empty.
        """
        if not self.task_queue:
            return None
        return self.task_queue[0]

    def has_pending_tasks(self) -> bool:
        """Check if there are tasks remaining in the queue.

        Returns:
            True if task_queue is not empty, False otherwise.
        """
        return len(self.task_queue) > 0

    # ========================================================================
    # Termination Methods
    # ========================================================================

    def terminate(
        self,
        reason: str,
        detail: str = "",
        cascade: bool = False,
    ) -> None:
        """Terminate this agent.

        Agent termination occurs in three main scenarios:
        1. Budget Depletion: Agent's budget drops to zero or below
        2. Objective Completion: Agent successfully completes and reports to supervisor
        3. Failed Subtask: Agent fails a critical subtask and supervisor decides not to reassign

        Args:
            reason: The termination reason (use TerminationReason constants).
            detail: Additional human-readable details about the termination.
            cascade: Whether this termination should cascade to child agents.
        """
        terminated_event = AgentTerminated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            reason=reason,
            detail=detail,
            final_budget=self.current_budget,
            cascade=cascade,
        )
        self._apply(terminated_event)
        self._changes.append(terminated_event)

    def abort_subtree(self, child_ids: list[UUID], reason: str) -> None:
        """Abort an entire subtree of child agents.

        When a supervisor determines that a subtask cannot be completed,
        it aborts the subtask and all agents in that subtree.

        Args:
            child_ids: List of child agent IDs to terminate.
            reason: Why the subtree is being aborted.
        """
        abort_event = SubtreeAborted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_ids_to_terminate=child_ids,
            reason=reason,
        )
        self._apply(abort_event)
        self._changes.append(abort_event)

    def check_budget_depletion(self) -> bool:
        """Check if budget is depleted and terminate if so.

        Returns:
            True if agent was terminated due to budget depletion.
        """
        if self.current_budget <= 0:
            self.terminate(
                reason=TerminationReason.BUDGET_DEPLETED,
                detail=f"Budget depleted: {self.current_budget}",
                cascade=True,
            )
            return True
        return False

    # ========================================================================
    # Budget Recollection Methods (Reward/Penalty Mechanism)
    # ========================================================================

    def recollect_budget_from_child(
        self,
        child_id: UUID,
        allocated_budget: float,
        remaining_budget: float,
        child_succeeded: bool,
        reward_ratio: float = 0.2,
        penalty_ratio: float = 0.1,
    ) -> float:
        """Recollect budget from a completed child with reward/penalty applied.

        Per the flowchart:
        - SUCCESS: supervisor.budget += remaining_budget + (reward_ratio * allocated_budget)
        - FAILURE: supervisor.budget += remaining_budget - (penalty_ratio * allocated_budget)

        The remaining budget is always returned, then the reward/penalty is applied
        as an additional adjustment based on the originally allocated budget.

        Note: The reward_ratio and penalty_ratio are heuristics to be tuned.
        Default values are placeholders:
        - reward_ratio: 0.2 (gain 20% of allocated budget on success)
        - penalty_ratio: 0.1 (lose 10% of allocated budget on failure)

        Args:
            child_id: The ID of the child agent.
            allocated_budget: The budget originally allocated to the child (X_Y2).
            remaining_budget: The child's remaining budget at completion.
            child_succeeded: Whether the child completed successfully.
            reward_ratio: Ratio of allocated_budget added on success (default 0.2).
            penalty_ratio: Ratio of allocated_budget subtracted on failure (default 0.1).

        Returns:
            The net amount of budget change for the supervisor.
        """
        if child_succeeded:
            # Success: return remaining + bonus
            bonus = reward_ratio * allocated_budget
            amount_recollected = remaining_budget + bonus
            ratio = reward_ratio
        else:
            # Failure: return remaining - penalty
            penalty = penalty_ratio * allocated_budget
            amount_recollected = remaining_budget - penalty
            ratio = penalty_ratio

        recollect_event = BudgetRecollected(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            original_allocation=allocated_budget,
            remaining_budget=remaining_budget,
            ratio_applied=ratio,
            amount_recollected=amount_recollected,
            child_succeeded=child_succeeded,
        )
        self._apply(recollect_event)
        self._changes.append(recollect_event)

        return amount_recollected

    def handle_child_failure(
        self,
        child_id: UUID,
        failure_reason: str,
        budget_at_failure: float,
        retry_attempted: bool = False,
    ) -> None:
        """Handle a child agent's failure.

        This is distinct from handle_child_update (success) and is used for
        tracking failure analytics and determining penalty ratios.

        Args:
            child_id: UUID of the child agent that failed.
            failure_reason: The reason for the failure.
            budget_at_failure: The child's remaining budget when it failed.
            retry_attempted: Whether a retry was attempted before recording failure.
        """
        assert self.role in (AgentRole.MANAGER, AgentRole.BOSS), (
            f"Requires BOSS/MANAGER, got {self.role}"
        )
        assert len(self.child_ids) > 0, "Requires agent with children"
        assert self.status == AgentStatus.WAITING, f"Requires WAITING status, got {self.status}"
        assert child_id in self.child_ids, f"child_id {child_id} not in spawned children"

        failed_event = ChildFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            failure_reason=failure_reason,
            budget_at_failure=budget_at_failure,
            retry_attempted=retry_attempted,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)

        # Check if all children are accounted for (completed + failed)
        total_done = len(self.child_results) + len(self.child_failures)
        if total_done == len(self.child_ids):
            # All children done - if any failed, parent fails too
            failed_reasons = [f"{cid}: {reason}" for cid, reason in self.child_failures.items()]
            self.fail_with_reason(f"Child agents failed: {'; '.join(failed_reasons)}")

    def record_all_children_failed(
        self,
        subtask_description: str,
        child_ids: list[UUID],
        penalty_ratio: float = 0.0,
    ) -> None:
        """Record that all children failed a subtask.

        When all subordinate nodes fail, the supervisor creates a penalty on all.

        Args:
            subtask_description: Description of the failed subtask.
            child_ids: List of child agent IDs that all failed.
            penalty_ratio: The penalty ratio applied to budget recollection.
        """
        all_failed_event = AllChildrenFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask_description=subtask_description,
            child_ids=child_ids,
            penalty_ratio=penalty_ratio,
        )
        self._apply(all_failed_event)
        self._changes.append(all_failed_event)

    # ========================================================================
    # Verification Methods
    # ========================================================================

    def inject_verification_task(
        self,
        target_subtask: Subtask,
        target_child_id: UUID,
        injection_reason: str,
        estimated_cost: float = 0.0,
    ) -> None:
        """Inject a verification task into the queue.

        The supervisor injects verification tasks based on heuristics to validate
        completed work and catch potential false negatives.

        Args:
            target_subtask: The subtask being verified (just completed).
            target_child_id: The child that completed the subtask being verified.
            injection_reason: Why this verification was injected.
            estimated_cost: Expected budget cost for verification.
        """
        inject_event = VerificationInjected(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            target_subtask=target_subtask,
            target_child_id=target_child_id,
            injection_reason=injection_reason,
            estimated_verification_cost=estimated_cost,
        )
        self._apply(inject_event)
        self._changes.append(inject_event)

    def spawn_verifier(
        self,
        verifier_id: UUID,
        target_subtask: Subtask,
        target_child_id: UUID,
        verifier_config: dict[str, Any],
    ) -> None:
        """Spawn an independent verifier sub-agent.

        The verifier agent is expected to be different from all original
        subordinate nodes (often a more advanced model).

        Args:
            verifier_id: UUID for the new verifier agent.
            target_subtask: The subtask being verified.
            target_child_id: The child whose work is being verified.
            verifier_config: Configuration for the verifier agent.
        """
        spawn_event = VerifierSpawned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            verifier_id=verifier_id,
            target_subtask=target_subtask,
            target_child_id=target_child_id,
            verifier_config=verifier_config,
        )
        self._apply(spawn_event)
        self._changes.append(spawn_event)

    def complete_verification(
        self,
        verifier_id: UUID,
        target_subtask: Subtask,
        target_child_id: UUID,
        verification_passed: bool,
        verification_report: str = "",
        issues_found: list[str] | None = None,
    ) -> None:
        """Record completion of a verification task.

        Args:
            verifier_id: UUID of the verifier agent.
            target_subtask: The subtask that was verified.
            target_child_id: The child whose work was verified.
            verification_passed: Whether the original work passed verification.
            verification_report: Detailed report from the verifier.
            issues_found: List of issues found during verification.
        """
        complete_event = VerificationCompleted(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            verifier_id=verifier_id,
            target_subtask=target_subtask,
            target_child_id=target_child_id,
            verification_passed=verification_passed,
            verification_report=verification_report,
            issues_found=issues_found or [],
        )
        self._apply(complete_event)
        self._changes.append(complete_event)

    def reinject_failed_task(
        self,
        subtask: Subtask,
        verification_context: str,
        original_child_id: UUID,
        retry_count: int = 1,
    ) -> None:
        """Re-inject a task that failed verification for redo.

        If the verifier reports that the previous task was not correctly finished,
        the supervisor re-injects the same task with additional context.

        Args:
            subtask: The subtask being re-injected for redo.
            verification_context: Context from the verifier to help redo.
            original_child_id: The child that originally failed this task.
            retry_count: Number of times this task has been reinjected.
        """
        reinject_event = TaskReinjected(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
            verification_context=verification_context,
            original_child_id=original_child_id,
            retry_count=retry_count,
        )
        self._apply(reinject_event)
        self._changes.append(reinject_event)

    def record_verification_heuristic_evaluation(
        self,
        subtask_completed: Subtask,
        child_id: UUID,
        complexity_score: float,
        subtree_size: int,
        random_roll: float,
        random_threshold: float,
        time_since_last: float,
        edits_count: int,
        expected_edits_range: tuple[int, int],
        suspicious: bool,
        available_budget: float,
        verification_decided: bool,
        decision_reasoning: str,
    ) -> None:
        """Record the verification heuristic evaluation for audit trail.

        This provides an audit trail for debugging and tuning the heuristics.

        Args:
            subtask_completed: The subtask that just completed.
            child_id: The child that completed the subtask.
            complexity_score: Estimated complexity of the subtask (0.0-1.0).
            subtree_size: Number of agents in the subtree.
            random_roll: Random value used for probability check (0.0-1.0).
            random_threshold: Threshold for random verification injection.
            time_since_last: Seconds since last verification task.
            edits_count: Number of edits reported by the child.
            expected_edits_range: Expected range of edits for task complexity.
            suspicious: Whether the completion is flagged as suspicious.
            available_budget: Budget available for verification.
            verification_decided: Whether a verification task was injected.
            decision_reasoning: Human-readable explanation of the decision.
        """
        heuristic_event = VerificationHeuristicEvaluated(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask_completed=subtask_completed,
            child_id=child_id,
            complexity_score=complexity_score,
            subtree_size=subtree_size,
            random_roll=random_roll,
            random_threshold=random_threshold,
            time_since_last_verification=time_since_last,
            edits_count=edits_count,
            expected_edits_range=expected_edits_range,
            suspicious=suspicious,
            available_budget=available_budget,
            verification_decided=verification_decided,
            decision_reasoning=decision_reasoning,
        )
        self._apply(heuristic_event)
        self._changes.append(heuristic_event)

    # ========================================================================
    # Multi-Model Strategy Methods
    # ========================================================================

    def spawn_parallel_subordinates(
        self,
        subtask: Subtask,
        subordinate_configs: list[dict[str, Any]],
        total_budget: float = 0.0,
    ) -> None:
        """Spawn multiple subordinate nodes for the same subtask in parallel.

        Default behavior: Spawn 3 agents with different LLM models, each with
        equal budget split. Only one needs to succeed.

        Args:
            subtask: The subtask assigned to all subordinates.
            subordinate_configs: List of configs with child_id, model, method_hint, budget.
            total_budget: Total budget allocated for this subtask.
        """
        spawn_event = SubordinatesSpawned(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
            total_budget_allocated=total_budget,
            subordinate_configs=subordinate_configs,
        )
        self._apply(spawn_event)
        self._changes.append(spawn_event)

    def record_first_success(
        self,
        winning_child_id: UUID,
        subtask: Subtask,
        sibling_ids_terminated: list[UUID],
        result: str = "",
        method_used: str = "",
        budget_recollected_from_winner: float = 0.0,
        budget_recollected_from_siblings: float = 0.0,
    ) -> None:
        """Record when the first parallel subordinate succeeds.

        When multiple subordinates work on the same subtask, the first to succeed
        triggers termination of all siblings to save budget.

        Args:
            winning_child_id: The child that succeeded first.
            subtask: The subtask that was completed.
            sibling_ids_terminated: List of sibling IDs that were terminated.
            result: The result produced by the winning child.
            method_used: The method/approach used by the winning child.
            budget_recollected_from_winner: Budget recollected with reward ratio.
            budget_recollected_from_siblings: Budget recollected from terminated siblings.
        """
        success_event = FirstSuccessRecorded(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            winning_child_id=winning_child_id,
            subtask=subtask,
            sibling_ids_terminated=sibling_ids_terminated,
            result=result,
            method_used=method_used,
            budget_recollected_from_winner=budget_recollected_from_winner,
            budget_recollected_from_siblings=budget_recollected_from_siblings,
        )
        self._apply(success_event)
        self._changes.append(success_event)

        # Clear current subtask tracking
        self.current_subtask = None
        self.current_subtask_subordinates = []

    def record_all_subordinates_failed(
        self,
        subtask: Subtask,
        failed_child_ids: list[UUID],
        failure_reasons: dict[str, str],
        total_budget_lost: float = 0.0,
    ) -> None:
        """Record when all parallel subordinates fail a subtask.

        This is a terminal condition for the subtask. The supervisor must decide
        whether to retry or report failure.

        Args:
            subtask: The subtask that all subordinates failed.
            failed_child_ids: List of all subordinates that failed.
            failure_reasons: Map of child_id (as string) -> failure reason.
            total_budget_lost: Total budget consumed by all failed subordinates.
        """
        failed_event = AllSubordinatesFailed(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            subtask=subtask,
            failed_child_ids=failed_child_ids,
            failure_reasons=failure_reasons,
            total_budget_lost=total_budget_lost,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)

        # Clear current subtask tracking
        self.current_subtask = None
        self.current_subtask_subordinates = []

    def retry_subtask(
        self,
        original_subtask: Subtask,
        revised_subtask: Subtask,
        revision_reason: str,
        additional_context: str = "",
    ) -> None:
        """Retry a failed subtask with a revised version.

        Re-inserts the revised subtask at the head of the task queue.

        Args:
            original_subtask: The original subtask that failed.
            revised_subtask: The revised subtask with additional context.
            revision_reason: Why the subtask was revised.
            additional_context: Context from failures/verification to help retry.
        """
        # Track retry count
        retry_count = self.subtask_retry_counts.get(original_subtask.description, 0) + 1
        self.subtask_retry_counts[original_subtask.description] = retry_count

        retry_event = SubtaskRetried(
            aggregate_id=self.session_id,
            sequence_number=self._next_sequence(),
            original_subtask=original_subtask,
            revised_subtask=revised_subtask,
            retry_count=retry_count,
            revision_reason=revision_reason,
            additional_context=additional_context,
        )
        self._apply(retry_event)
        self._changes.append(retry_event)
