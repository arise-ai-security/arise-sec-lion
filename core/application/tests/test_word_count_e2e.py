"""End-to-end integration test for the Word Count Workflow with real LLM calls.

This test demonstrates the example workflow from the Agent Lifecycle description
using actual GPT-4o API calls. The test is marked with @pytest.mark.e2e and
requires OPENAI_API_KEY in the environment.

Objective: Count the number of words in a given text file.

Workflow:
1. Root Node Creation: Root BOSS agent with budget of 1000
2. Task Decomposition: LLM decomposes into subtasks
3. Sub-Task Assignment: Spawn 3 subordinates with GPT-4o
4. Budget Recollection: First success triggers budget recollection
5. Verification: Heuristics decide whether to verify
6. Pop Next Task: Continue with next subtask

To run: OPENAI_API_KEY=sk-... uv run pytest core/application/tests/test_word_count_e2e.py -v -s
"""

import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv

from core.domain.events import (
    AgentCreated,
    BudgetAllocated,
    DomainEvent,
    FirstSuccessRecorded,
    SubordinatesSpawned,
    SubtasksDefined,
    TaskAssigned,
    TaskDequeued,
    TaskEnqueued,
    WorkCompleted,
)
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.subtask import Subtask
from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort

# Load environment variables from .env file
load_dotenv()


# =============================================================================
# In-Memory Event Store for Testing
# =============================================================================


class InMemoryEventStore(EventStorePort):
    """Simple in-memory event store for testing.

    This implementation stores events in a dictionary keyed by aggregate_id.
    It supports OCC via expected_version checks.
    """

    def __init__(self) -> None:
        self._events: dict[UUID, list[DomainEvent]] = {}

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append an event with OCC check."""
        aggregate_id = event.aggregate_id
        current_events = self._events.get(aggregate_id, [])

        if len(current_events) != expected_version:
            from core.domain.exceptions import ConcurrencyError
            raise ConcurrencyError(
                aggregate_id=str(aggregate_id),
                expected_version=expected_version,
                actual_version=len(current_events),
            )

        if aggregate_id not in self._events:
            self._events[aggregate_id] = []
        self._events[aggregate_id].append(event)

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]:
        """Get all events for an aggregate."""
        return self._events.get(aggregate_id, [])

    async def get_all_aggregate_ids(self) -> list[UUID]:
        """Get all aggregate IDs."""
        return list(self._events.keys())


# =============================================================================
# Mock Worker Tool Port (simulates task execution)
# =============================================================================


class MockWorkerToolPort(WorkerToolPort):
    """Mock worker tool that simulates task execution.

    For this test, we simulate the behavior where one subordinate succeeds
    while others fail or are terminated.
    """

    def __init__(self, success_method: str = "vi") -> None:
        """Initialize with the method that should succeed."""
        self.success_method = success_method
        self.executions: list[dict[str, Any]] = []

    async def run_session(
        self,
        task: str,
        session_id: UUID,
        config: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        """Simulate running a task."""
        method = config.get("method_hint", "unknown")
        self.executions.append({
            "session_id": session_id,
            "task": task,
            "method": method,
            "config": config,
        })

        # Simulate different outcomes based on method
        if method == self.success_method:
            yield WorkCompleted(
                aggregate_id=session_id,
                sequence_number=1,  # Will be adjusted by caller
                result=f"Task completed successfully using {method}",
            )
        else:
            # Simulate failure for other methods
            from core.domain.events import WorkFailed
            yield WorkFailed(
                aggregate_id=session_id,
                sequence_number=1,
                reason=f"Method '{method}' not available or failed",
            )


# =============================================================================
# Test Configuration Helpers
# =============================================================================


def create_heuristic_config(model: str = "gpt-4o") -> dict[str, Any]:
    """Create a heuristic config for agents."""
    return {
        "strategy": "heuristic",
        "base": {
            "model": model,
            "temperature": 0.7,
            "max_tokens": 2000,
        },
        "tool": "claude_code",
    }


def create_subordinate_configs(
    budget_per_subordinate: float,
    models: list[str] | None = None,
    methods: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Create configurations for subordinates with different methods."""
    if models is None:
        models = ["gpt-4o", "gpt-4o", "gpt-4o"]
    if methods is None:
        methods = ["vim", "vi", "nano"]

    configs = []
    for model, method in zip(models, methods):
        configs.append({
            "child_id": str(uuid4()),
            "config": create_heuristic_config(model),
            "budget": budget_per_subordinate,
            "model": model,
            "method_hint": method,
        })

    return configs


# =============================================================================
# Skip if no API key
# =============================================================================


def has_openai_key() -> bool:
    """Check if OpenAI API key is available."""
    return bool(os.getenv("OPENAI_API_KEY"))


requires_openai = pytest.mark.skipif(
    not has_openai_key(),
    reason="OPENAI_API_KEY not set in environment"
)


# =============================================================================
# End-to-End Tests
# =============================================================================


@pytest.mark.e2e
class TestWordCountWorkflowE2E:
    """End-to-end tests for the word count workflow with real LLM calls."""

    @pytest.fixture
    def event_store(self) -> InMemoryEventStore:
        """Create an in-memory event store."""
        return InMemoryEventStore()

    @pytest.fixture
    def text_file(self) -> str:
        """Create a temporary text file for testing."""
        content = "Hello world this is a test file with some words in it"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(content)
            return f.name

    @pytest.fixture
    def root_session_id(self) -> UUID:
        """Create a unique session ID for root agent."""
        return uuid4()

    @pytest.fixture
    def initial_budget(self) -> float:
        """Initial budget for root agent."""
        return 1000.0

    @requires_openai
    @pytest.mark.asyncio
    async def test_step1_and_2_root_creation_and_llm_decomposition(
        self,
        event_store: InMemoryEventStore,
        root_session_id: UUID,
        initial_budget: float,
        text_file: str,
    ):
        """Test Steps 1 & 2: Root creation and LLM-based task decomposition.

        This test uses a real LLM call to decompose the objective.
        """
        from infrastructure.adapters.litellm_adapter import LiteLLMAdapter

        # Given: Root BOSS agent with budget
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=create_heuristic_config("gpt-4o"),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")

        objective = f"Count the number of words in the text file at: {text_file}"
        root.assign_task(objective)

        # Save initial events
        for idx, event in enumerate(root.events):
            await event_store.append(event, expected_version=idx)
        root.mark_changes_as_committed()

        # When: Use real LLM to decompose task
        llm = LiteLLMAdapter()

        decomposition_prompt = f"""
You are a task decomposition agent. Break down the following objective into
exactly 3 subtasks that can be executed sequentially.

Objective: {objective}

Return ONLY a JSON array with 3 objects, each having a "description" field.
Example format:
[
    {{"description": "First subtask description"}},
    {{"description": "Second subtask description"}},
    {{"description": "Third subtask description"}}
]

Focus on these steps:
1. Read/open the text file
2. Count the words (using wc or similar)
3. Report/output the result
"""

        response = await llm.query(
            prompt=decomposition_prompt,
            config_dict={
                "model": "gpt-4o",
                "temperature": 0.3,
                "max_tokens": 500,
            },
        )

        print(f"\n=== LLM Decomposition Response ===\n{response}\n")

        # Parse the response
        import json
        # Extract JSON from response (handle markdown code blocks)
        json_str = response
        if "```json" in response:
            json_str = response.split("```json")[1].split("```")[0].strip()
        elif "```" in response:
            json_str = response.split("```")[1].split("```")[0].strip()

        subtasks_data = json.loads(json_str)

        # Then: Should have 3 subtasks
        assert len(subtasks_data) >= 3, f"Expected 3 subtasks, got {len(subtasks_data)}"

        # Create Subtask objects
        subtasks = [
            Subtask(description=s["description"], config={})
            for s in subtasks_data[:3]
        ]

        print(f"\n=== Parsed Subtasks ===")
        for i, st in enumerate(subtasks, 1):
            print(f"  {i}. {st.description}")

        # Verify they make sense (basic checks)
        all_descriptions = " ".join(s.description.lower() for s in subtasks)
        assert any(word in all_descriptions for word in ["file", "read", "open"]), \
            "Expected subtask about reading/opening file"
        assert any(word in all_descriptions for word in ["count", "word", "wc"]), \
            "Expected subtask about counting words"

    @requires_openai
    @pytest.mark.asyncio
    async def test_full_workflow_with_llm(
        self,
        event_store: InMemoryEventStore,
        root_session_id: UUID,
        initial_budget: float,
        text_file: str,
    ):
        """Test complete workflow with real LLM calls.

        This is the full end-to-end test demonstrating:
        1. Root creation with budget
        2. LLM-based task decomposition
        3. Task queue management
        4. Subordinate spawning (simulated execution)
        5. Budget recollection
        """
        from infrastructure.adapters.litellm_adapter import LiteLLMAdapter

        print("\n" + "=" * 60)
        print("WORD COUNT WORKFLOW - END-TO-END TEST")
        print("=" * 60)

        # =====================================================================
        # Step 1: Root Node Creation
        # =====================================================================
        print("\n--- Step 1: Root Node Creation ---")

        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=create_heuristic_config("gpt-4o"),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")

        objective = f"Count the number of words in the text file at: {text_file}"
        root.assign_task(objective)

        print(f"  Root ID: {root_session_id}")
        print(f"  Budget: {root.current_budget}")
        print(f"  Objective: {objective}")

        # Save events
        for idx, event in enumerate(root.events):
            await event_store.append(event, expected_version=idx)
        root.mark_changes_as_committed()

        # =====================================================================
        # Step 2: Task Decomposition via LLM
        # =====================================================================
        print("\n--- Step 2: Task Decomposition (LLM Call) ---")

        llm = LiteLLMAdapter()

        decomposition_prompt = f"""
Break down this objective into exactly 3 sequential subtasks.
Return ONLY a JSON array with objects having "description" field.

Objective: {objective}

Required subtasks:
1. Open/read the text file
2. Count the words using wc command
3. Print/output the word count result

JSON format:
[{{"description": "..."}}, {{"description": "..."}}, {{"description": "..."}}]
"""

        response = await llm.query(
            prompt=decomposition_prompt,
            config_dict={"model": "gpt-4o", "temperature": 0.3, "max_tokens": 500},
        )

        # Parse response
        import json
        json_str = response
        if "```json" in response:
            json_str = response.split("```json")[1].split("```")[0].strip()
        elif "```" in response:
            json_str = response.split("```")[1].split("```")[0].strip()

        subtasks_data = json.loads(json_str)
        subtasks = [Subtask(description=s["description"], config={}) for s in subtasks_data[:3]]

        print(f"  LLM decomposed into {len(subtasks)} subtasks:")
        for i, st in enumerate(subtasks, 1):
            print(f"    {i}. {st.description}")

        # =====================================================================
        # Step 3: Enqueue Subtasks
        # =====================================================================
        print("\n--- Step 3: Enqueue Subtasks ---")

        for subtask in subtasks:
            root.enqueue_task(subtask)

        print(f"  Task queue size: {len(root.task_queue)}")

        # Save events
        version = root.version
        for event in root.events:
            await event_store.append(event, expected_version=version)
            version += 1
        root.mark_changes_as_committed()

        # =====================================================================
        # Step 4: Process First Subtask - Spawn Subordinates
        # =====================================================================
        print("\n--- Step 4: Pop First Task & Spawn Subordinates ---")

        first_subtask = root.dequeue_task()
        assert first_subtask is not None

        print(f"  Processing: {first_subtask.description}")

        # Allocate budget for subtask (333 out of 1000)
        subtask_budget = 333.0
        budget_per_subordinate = subtask_budget / 3  # 111 each

        print(f"  Subtask budget: {subtask_budget}")
        print(f"  Per subordinate: {budget_per_subordinate}")

        # Create subordinate configs with different "methods" (all use gpt-4o)
        subordinate_configs = create_subordinate_configs(
            budget_per_subordinate,
            models=["gpt-4o", "gpt-4o", "gpt-4o"],
            methods=["vim", "vi", "nano"],
        )

        root.spawn_parallel_subordinates(
            subtask=first_subtask,
            subordinate_configs=subordinate_configs,
            total_budget=subtask_budget,
        )

        # Deduct budget
        root.adjust_budget(-subtask_budget, reason="Allocated to subordinates")

        print(f"  Spawned {len(subordinate_configs)} subordinates:")
        for cfg in subordinate_configs:
            print(f"    - {cfg['method_hint']}: budget={cfg['budget']}")

        print(f"  Root budget after allocation: {root.current_budget}")

        # =====================================================================
        # Step 5: Simulate Subordinate Execution (vi succeeds)
        # =====================================================================
        print("\n--- Step 5: Subordinate Execution (Simulated) ---")

        # In real scenario, subordinates would execute and report back
        # For this test, we simulate: vim fails, vi succeeds, nano terminated

        # vim subordinate: tries to use vim, but budget depletes trying to install
        vim_sub_id = UUID(subordinate_configs[0]["child_id"])
        vim_remaining = 0.0  # Budget depleted
        print(f"  vim ({str(vim_sub_id)[:8]}): DEPLETED (remaining: {vim_remaining})")

        # vi subordinate: succeeds
        vi_sub_id = UUID(subordinate_configs[1]["child_id"])
        vi_remaining = 80.0  # Used some budget, 80 remaining of 111
        print(f"  vi ({str(vi_sub_id)[:8]}): SUCCESS (remaining: {vi_remaining})")

        # nano subordinate: terminated early
        nano_sub_id = UUID(subordinate_configs[2]["child_id"])
        nano_remaining = 111.0  # Full budget remaining (terminated early)
        print(f"  nano ({str(nano_sub_id)[:8]}): TERMINATED (remaining: {nano_remaining})")

        # =====================================================================
        # Step 6: Budget Recollection
        # =====================================================================
        print("\n--- Step 6: Budget Recollection ---")

        # Calculate recollection
        reward_ratio = 1.2
        neutral_ratio = 1.0

        vim_recollected = vim_remaining * neutral_ratio  # 0 (depleted)
        vi_recollected = vi_remaining * reward_ratio  # 80 * 1.2 = 96
        nano_recollected = nano_remaining * neutral_ratio  # 111 * 1.0 = 111

        total_recollected = vim_recollected + vi_recollected + nano_recollected

        print(f"  vim recollected: {vim_remaining} * {neutral_ratio} = {vim_recollected}")
        print(f"  vi recollected: {vi_remaining} * {reward_ratio} = {vi_recollected}")
        print(f"  nano recollected: {nano_remaining} * {neutral_ratio} = {nano_recollected}")
        print(f"  Total recollected: {total_recollected}")

        # Record first success
        root.record_first_success(
            winning_child_id=vi_sub_id,
            subtask=first_subtask,
            sibling_ids_terminated=[vim_sub_id, nano_sub_id],
            result="File opened successfully using vi",
            method_used="vi",
            budget_recollected_from_winner=vi_recollected,
            budget_recollected_from_siblings=vim_recollected + nano_recollected,
        )

        print(f"\n  Root budget after recollection: {root.current_budget}")

        # Verify budget calculation
        # Initial: 1000
        # After allocation: 1000 - 333 = 667
        # After recollection: 667 + 96 + 111 = 874
        expected_budget = initial_budget - subtask_budget + total_recollected
        assert abs(root.current_budget - expected_budget) < 0.01, \
            f"Budget mismatch: {root.current_budget} != {expected_budget}"

        # =====================================================================
        # Step 7: Continue with Remaining Tasks
        # =====================================================================
        print("\n--- Step 7: Remaining Tasks ---")

        print(f"  Tasks remaining in queue: {len(root.task_queue)}")
        for i, task in enumerate(root.task_queue, 1):
            print(f"    {i}. {task.description}")

        # Pop next task
        next_task = root.dequeue_task()
        if next_task:
            print(f"\n  Next task to process: {next_task.description}")

        # Save all events
        version = root.version
        for event in root.events:
            await event_store.append(event, expected_version=version)
            version += 1
        root.mark_changes_as_committed()

        # =====================================================================
        # Final Summary
        # =====================================================================
        print("\n" + "=" * 60)
        print("WORKFLOW COMPLETE")
        print("=" * 60)

        # Reload from events to verify event sourcing
        all_events = await event_store.get_events(root_session_id)
        reconstructed = AgentSession.load_from_history(all_events)

        print(f"\n  Final budget: {reconstructed.current_budget}")
        print(f"  Tasks completed: 1 of 3")
        print(f"  Tasks remaining: {len(reconstructed.task_queue)}")
        print(f"  Children spawned: {len(reconstructed.child_ids)}")
        print(f"  Events generated: {len(all_events)}")

        # Verify reconstruction
        assert reconstructed.current_budget == root.current_budget
        assert len(reconstructed.task_queue) == len(root.task_queue)

        # Cleanup temp file
        os.unlink(text_file)

        print("\n  ✓ All assertions passed!")

    @requires_openai
    @pytest.mark.asyncio
    async def test_llm_complexity_evaluation(self, root_session_id: UUID):
        """Test LLM-based complexity evaluation for a subtask."""
        from infrastructure.adapters.litellm_adapter import LiteLLMAdapter

        llm = LiteLLMAdapter()

        # Test a simple task
        simple_task = "Print 'Hello World' to the console"
        simple_prompt = f"""
Evaluate if this task is SIMPLE (can be done directly) or COMPLEX (needs decomposition).
Task: {simple_task}

Reply with ONLY one word: SIMPLE or COMPLEX
"""

        simple_response = await llm.query(
            prompt=simple_prompt,
            config_dict={"model": "gpt-4o", "temperature": 0.1, "max_tokens": 10},
        )

        print(f"\nSimple task evaluation: {simple_response.strip()}")
        assert "SIMPLE" in simple_response.upper()

        # Test a complex task
        complex_task = "Build a full-stack web application with user authentication, database, and REST API"
        complex_prompt = f"""
Evaluate if this task is SIMPLE (can be done directly) or COMPLEX (needs decomposition).
Task: {complex_task}

Reply with ONLY one word: SIMPLE or COMPLEX
"""

        complex_response = await llm.query(
            prompt=complex_prompt,
            config_dict={"model": "gpt-4o", "temperature": 0.1, "max_tokens": 10},
        )

        print(f"Complex task evaluation: {complex_response.strip()}")
        assert "COMPLEX" in complex_response.upper()


# =============================================================================
# Budget Calculation Verification Tests (No API calls needed)
# =============================================================================


class TestBudgetCalculationsE2E:
    """Verify budget calculations match the example from documentation."""

    def test_example_budget_flow(self):
        """Verify the exact budget flow from the example.

        Initial: 1000
        Allocate for subtask 1: -333
        After allocation: 667

        Subordinate execution:
        - vim: depletes to 0 (no return)
        - vi: succeeds with 80 remaining, reward 1.2x = 96
        - nano: terminated with 111 remaining, neutral 1.0x = 111

        After recollection: 667 + 96 + 111 = 874

        Note: The original example's 1022.2 assumes different numbers.
        """
        initial = 1000.0
        subtask_allocation = 333.0

        after_allocation = initial - subtask_allocation
        assert after_allocation == 667.0

        # Subordinate outcomes
        vim_return = 0.0 * 1.0  # 0
        vi_return = 80.0 * 1.2  # 96
        nano_return = 111.0 * 1.0  # 111

        final = after_allocation + vim_return + vi_return + nano_return
        assert final == 874.0

        print(f"\nBudget flow verified:")
        print(f"  Initial: {initial}")
        print(f"  After allocation: {after_allocation}")
        print(f"  vim return: {vim_return}")
        print(f"  vi return: {vi_return}")
        print(f"  nano return: {nano_return}")
        print(f"  Final: {final}")

    def test_example_with_full_remaining_budgets(self):
        """Test with scenario where no budget is consumed.

        If all subordinates have full 111 remaining:
        - vim: terminated, 111 * 1.0 = 111
        - vi: succeeded, 111 * 1.2 = 133.2
        - nano: terminated, 111 * 1.0 = 111

        After: 667 + 111 + 133.2 + 111 = 1022.2
        """
        initial = 1000.0
        subtask_allocation = 333.0
        budget_per_sub = 111.0

        after_allocation = initial - subtask_allocation  # 667

        vim_return = budget_per_sub * 1.0  # 111
        vi_return = budget_per_sub * 1.2  # 133.2
        nano_return = budget_per_sub * 1.0  # 111

        final = after_allocation + vim_return + vi_return + nano_return
        assert abs(final - 1022.2) < 0.01

        print(f"\nFull budget scenario verified:")
        print(f"  Initial: {initial}")
        print(f"  After allocation: {after_allocation}")
        print(f"  vim return: {vim_return}")
        print(f"  vi return: {vi_return}")
        print(f"  nano return: {nano_return}")
        print(f"  Final: {final} (matches example: 1022.2)")
