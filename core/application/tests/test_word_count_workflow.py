"""Integration test for the Word Count Workflow.

This test demonstrates the example workflow from the Agent Lifecycle description:

Objective: Count the number of words in a given text file.

1. Root Node Creation: A root agent node is created with budget of 1000.
2. Task Decomposition: Root decomposes into 3 subtasks (333 budget each):
   - Sub-task 1: Open the text file.
   - Sub-task 2: Run `wc` word count.
   - Sub-task 3: Print the result.
3. Sub-Task Assignment:
   - Sub-task 1 spawns 3 subordinates with different models and methods (vim, vi, nano)
   - Each subordinate gets 111 budget
4. Budget Recollection:
   - Successful subordinate (vi): budget recollected with 1.2x ratio
   - Terminated siblings: budget recollected with 1.0x ratio (no reward/penalty)
5. Verification: Supervisor may inject verification task based on heuristics
6. Pop Next Task: Continue with next subtask in queue
"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from core.domain.events import (
    AgentCreated,
    AllSubordinatesFailed,
    BudgetAllocated,
    FirstSuccessRecorded,
    SubordinatesSpawned,
    SubtasksDefined,
    TaskDequeued,
)
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.subtask import Subtask


def _create_heuristic_config(model: str = "gpt-4o") -> dict[str, Any]:
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


def _create_boss_config() -> dict[str, Any]:
    """Create configuration for the root BOSS agent using heuristic strategy."""
    return _create_heuristic_config("gpt-4o")  # Powerful model for root


def _create_subordinate_configs(
    subtask: Subtask,  # noqa: ARG001 - kept for API consistency
    budget_per_subordinate: float,
) -> list[dict[str, Any]]:
    """Create configurations for 3 subordinates with different models.

    Args:
        subtask: The subtask to assign (kept for API consistency).
        budget_per_subordinate: Budget to allocate to each subordinate.

    Returns:
        List of subordinate configurations with different models.
    """
    models = [
        ("claude-sonnet-4-5-20250514", "vim"),
        ("gemini-1.5-pro", "vi"),
        ("gpt-4o", "nano"),
    ]

    configs = []
    for model, method in models:
        configs.append({
            "child_id": str(uuid4()),
            "config": _create_heuristic_config(model),
            "budget": budget_per_subordinate,
            "model": model,
            "method_hint": method,
        })

    return configs


class TestWordCountWorkflow:
    """Integration tests for the word count workflow example."""

    @pytest.fixture
    def root_session_id(self) -> UUID:
        """Create a unique session ID for root agent."""
        return uuid4()

    @pytest.fixture
    def initial_budget(self) -> float:
        """Initial budget for root agent."""
        return 1000.0

    def test_step1_root_node_creation(self, root_session_id: UUID, initial_budget: float):
        """Test Step 1: Root node is created with budget of 1000."""
        # Given: A root BOSS agent with initial budget
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")

        # Then: Root should be BOSS with correct budget
        assert root.role == AgentRole.BOSS
        assert root.current_budget == initial_budget
        assert root.parent_id is None
        assert root.status == AgentStatus.PENDING

        # Verify events
        events = root.events
        assert any(isinstance(e, AgentCreated) for e in events)
        assert any(isinstance(e, BudgetAllocated) for e in events)

    def test_step2_task_decomposition(self, root_session_id: UUID, initial_budget: float):
        """Test Step 2: Root decomposes objective into 3 subtasks."""
        # Given: Root agent with task assigned
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")
        root.assign_task("Count the number of words in a given text file.")

        # When: Root decomposes task into subtasks
        subtasks = [
            Subtask(description="Open the text file.", config={}),
            Subtask(description="Run `wc` word count.", config={}),
            Subtask(description="Print the result.", config={}),
        ]

        # Simulate LLM response processing - create and apply SubtasksDefined event
        subtasks_event = SubtasksDefined(
            aggregate_id=root_session_id,
            sequence_number=root._next_sequence(),
            subtasks=subtasks,
        )
        root._apply(subtasks_event)
        root._changes.append(subtasks_event)  # Add to uncommitted changes

        # Then: Should have 3 subtasks defined
        assert len(subtasks) == 3

        # Verify events include SubtasksDefined
        events = root.events
        subtasks_defined = [e for e in events if isinstance(e, SubtasksDefined)]
        assert len(subtasks_defined) == 1
        assert len(subtasks_defined[0].subtasks) == 3

    def test_step3_subtask_enqueued_and_spawning(self, root_session_id: UUID, initial_budget: float):
        """Test Step 3: Subtasks are enqueued and subordinates are spawned."""
        # Given: Root agent ready to process subtasks
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")
        root.assign_task("Count the number of words in a given text file.")

        # Define subtasks
        subtasks = [
            Subtask(description="Open the text file.", config={}),
            Subtask(description="Run `wc` word count.", config={}),
            Subtask(description="Print the result.", config={}),
        ]

        # Enqueue all subtasks
        for subtask in subtasks:
            root.enqueue_task(subtask)

        assert len(root.task_queue) == 3
        assert root.has_pending_tasks()

        # When: Pop first subtask and spawn 3 subordinates
        first_subtask = root.dequeue_task()
        assert first_subtask is not None
        assert first_subtask.description == "Open the text file."
        assert len(root.task_queue) == 2

        # Calculate budget per subordinate (333 / 3 = 111)
        subtask_budget = 333.0
        budget_per_subordinate = subtask_budget / 3  # 111

        # Create subordinate configs with different models
        subordinate_configs = _create_subordinate_configs(first_subtask, budget_per_subordinate)

        # Spawn parallel subordinates
        root.spawn_parallel_subordinates(
            subtask=first_subtask,
            subordinate_configs=subordinate_configs,
            total_budget=subtask_budget,
        )

        # Then: Should have spawned 3 subordinates
        assert len(root.current_subtask_subordinates) == 3
        assert root.current_subtask == first_subtask

        # Verify each subordinate has correct budget
        for config in subordinate_configs:
            child_id = UUID(config["child_id"])
            assert child_id in root.child_budgets
            assert root.child_budgets[child_id] == budget_per_subordinate

        # Verify SubordinatesSpawned event
        events = root.events
        spawned_events = [e for e in events if isinstance(e, SubordinatesSpawned)]
        assert len(spawned_events) == 1
        assert spawned_events[0].total_budget_allocated == subtask_budget
        assert len(spawned_events[0].subordinate_configs) == 3

    def test_step4_first_success_budget_recollection(self, root_session_id: UUID, initial_budget: float):
        """Test Step 4: Budget recollection when first subordinate succeeds.

        Scenario:
        - Subordinate 1 (vim): depletes budget trying to install vim
        - Subordinate 2 (vi): succeeds, recollected with 1.2x ratio
        - Subordinate 3 (nano): terminated, recollected with 1.0x ratio

        Expected: 1000 - 333 + (111 * 1.2) + 222 = 1022.2
        Note: Subordinate 1's budget is lost (depleted), subordinate 2 is rewarded,
        subordinate 3's remaining budget is recollected at 1.0x
        """
        # Given: Root agent with subtask in progress
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")
        root.assign_task("Count the number of words in a given text file.")

        subtask = Subtask(description="Open the text file.", config={})
        root.enqueue_task(subtask)
        _ = root.dequeue_task()

        # Spawn subordinates
        subtask_budget = 333.0
        budget_per_subordinate = 111.0
        subordinate_configs = _create_subordinate_configs(subtask, budget_per_subordinate)

        root.spawn_parallel_subordinates(
            subtask=subtask,
            subordinate_configs=subordinate_configs,
            total_budget=subtask_budget,
        )

        # Simulate deduction of budget for spawning
        root.adjust_budget(-subtask_budget, reason="Allocated to subordinates")

        # Get subordinate IDs
        sub1_id = UUID(subordinate_configs[0]["child_id"])  # vim - will deplete
        sub2_id = UUID(subordinate_configs[1]["child_id"])  # vi - will succeed
        sub3_id = UUID(subordinate_configs[2]["child_id"])  # nano - will be terminated

        # Simulate scenario:
        # - Sub1 depleted budget (remaining = 0)
        # - Sub2 succeeded (remaining = 111, reward ratio = 1.2x)
        # - Sub3 terminated early (remaining = 111, neutral ratio = 1.0x)

        # Sub1 depleted - no budget to recollect
        sub1_remaining = 0.0
        sub1_recollected = sub1_remaining * 1.0  # 0

        # Sub2 succeeded - reward ratio
        sub2_remaining = 111.0
        reward_ratio = 1.2
        sub2_recollected = sub2_remaining * reward_ratio  # 133.2

        # Sub3 terminated - neutral ratio
        sub3_remaining = 111.0
        neutral_ratio = 1.0
        sub3_recollected = sub3_remaining * neutral_ratio  # 111

        # When: Record first success and recollect budgets
        sibling_ids_terminated = [sub1_id, sub3_id]
        total_siblings_recollected = sub1_recollected + sub3_recollected  # 0 + 111 = 111

        root.record_first_success(
            winning_child_id=sub2_id,
            subtask=subtask,
            sibling_ids_terminated=sibling_ids_terminated,
            result="File opened successfully using vi",
            method_used="vi",
            budget_recollected_from_winner=sub2_recollected,
            budget_recollected_from_siblings=total_siblings_recollected,
        )

        # Then: Budget should be recalculated correctly
        # Initial: 1000
        # After allocation: 1000 - 333 = 667
        # After recollection: 667 + 133.2 + 111 = 911.2
        expected_budget = initial_budget - subtask_budget + sub2_recollected + total_siblings_recollected
        assert abs(root.current_budget - expected_budget) < 0.01

        # Verify FirstSuccessRecorded event
        events = root.events
        success_events = [e for e in events if isinstance(e, FirstSuccessRecorded)]
        assert len(success_events) == 1
        assert success_events[0].winning_child_id == sub2_id
        assert success_events[0].budget_recollected_from_winner == sub2_recollected
        assert success_events[0].budget_recollected_from_siblings == total_siblings_recollected

        # Current subtask should be cleared
        assert root.current_subtask is None
        assert len(root.current_subtask_subordinates) == 0

    def test_step5_verification_injection(self, root_session_id: UUID, initial_budget: float):
        """Test Step 5: Verification task injection based on heuristics."""
        # Given: Root agent that just completed a subtask
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")
        root.assign_task("Count the number of words in a given text file.")

        # Add remaining subtasks to queue
        subtasks = [
            Subtask(description="Run `wc` word count.", config={}),
            Subtask(description="Print the result.", config={}),
        ]
        for subtask in subtasks:
            root.enqueue_task(subtask)

        # Create a completed subtask for verification
        completed_subtask = Subtask(description="Open the text file.", config={})
        child_id = uuid4()

        # When: Supervisor decides to inject verification task (suspicious heuristic)
        root.inject_verification_task(
            target_subtask=completed_subtask,
            target_child_id=child_id,
            injection_reason="Task complexity high, random verification triggered",
            estimated_cost=50.0,
        )

        # Then: Should be in VERIFYING status with pending verification
        assert root.status == AgentStatus.VERIFYING
        assert child_id in root.verification_pending

        # Remaining tasks in queue unchanged
        assert len(root.task_queue) == 2

    def test_step6_pop_next_task(self, root_session_id: UUID, initial_budget: float):
        """Test Step 6: Pop next task from queue after completing first subtask."""
        # Given: Root agent with 2 remaining tasks in queue
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")

        subtasks = [
            Subtask(description="Run `wc` word count.", config={}),
            Subtask(description="Print the result.", config={}),
        ]
        for subtask in subtasks:
            root.enqueue_task(subtask)

        assert len(root.task_queue) == 2

        # When: Pop next task
        next_task = root.dequeue_task()

        # Then: Should get the wc command subtask
        assert next_task is not None
        assert next_task.description == "Run `wc` word count."
        assert len(root.task_queue) == 1

        # Verify TaskDequeued event
        events = root.events
        dequeue_events = [e for e in events if isinstance(e, TaskDequeued)]
        assert len(dequeue_events) == 1

    def test_all_subordinates_fail_retry_flow(self, root_session_id: UUID, initial_budget: float):
        """Test retry flow when all subordinates fail a subtask."""
        # Given: Root agent with subtask that all subordinates fail
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")

        subtask = Subtask(description="Open the text file.", config={})
        root.enqueue_task(subtask)
        _ = root.dequeue_task()

        subordinate_configs = _create_subordinate_configs(subtask, 111.0)
        root.spawn_parallel_subordinates(
            subtask=subtask,
            subordinate_configs=subordinate_configs,
            total_budget=333.0,
        )

        # Simulate all subordinates failing
        failed_child_ids = [UUID(c["child_id"]) for c in subordinate_configs]
        failure_reasons = {
            str(failed_child_ids[0]): "vim not available, budget depleted",
            str(failed_child_ids[1]): "vi encountered permission error",
            str(failed_child_ids[2]): "nano failed to open file",
        }

        # When: Record all subordinates failed
        root.record_all_subordinates_failed(
            subtask=subtask,
            failed_child_ids=failed_child_ids,
            failure_reasons=failure_reasons,
            total_budget_lost=333.0,
        )

        # Then: All failures should be recorded
        assert len(root.child_failures) == 3
        for child_id in failed_child_ids:
            assert child_id in root.child_failures

        # Verify AllSubordinatesFailed event
        events = root.events
        all_failed_events = [e for e in events if isinstance(e, AllSubordinatesFailed)]
        assert len(all_failed_events) == 1

        # When: Retry with revised subtask
        revised_subtask = Subtask(
            description="Open the text file using cat command instead",
            config={"additional_context": "Avoid using text editors"},
        )

        root.retry_subtask(
            original_subtask=subtask,
            revised_subtask=revised_subtask,
            revision_reason="All text editor approaches failed",
            additional_context="vim/vi/nano not available or errored",
        )

        # Then: Revised subtask should be at head of queue
        assert len(root.task_queue) == 1
        assert root.task_queue[0] == revised_subtask
        assert root.subtask_retry_counts[subtask.description] == 1

    def test_full_workflow_event_sourcing_reconstruction(
        self, root_session_id: UUID, initial_budget: float
    ):
        """Test that full workflow can be reconstructed from events."""
        # Given: Execute full workflow
        root = AgentSession.create(
            session_id=root_session_id,
            role=AgentRole.BOSS,
            config=_create_boss_config(),
            parent_id=None,
        )
        root.allocate_budget(initial_budget, source="initial")
        root.assign_task("Count the number of words in a given text file.")

        # Add subtasks
        subtasks = [
            Subtask(description="Open the text file.", config={}),
            Subtask(description="Run `wc` word count.", config={}),
            Subtask(description="Print the result.", config={}),
        ]
        for subtask in subtasks:
            root.enqueue_task(subtask)

        # Process first subtask
        first_subtask = root.dequeue_task()
        assert first_subtask is not None
        subordinate_configs = _create_subordinate_configs(first_subtask, 111.0)
        root.spawn_parallel_subordinates(
            subtask=first_subtask,
            subordinate_configs=subordinate_configs,
            total_budget=333.0,
        )
        root.adjust_budget(-333.0, reason="Allocated to subordinates")

        # Record success
        winner_id = UUID(subordinate_configs[1]["child_id"])
        sibling_ids = [UUID(subordinate_configs[0]["child_id"]), UUID(subordinate_configs[2]["child_id"])]
        root.record_first_success(
            winning_child_id=winner_id,
            subtask=first_subtask,
            sibling_ids_terminated=sibling_ids,
            result="File opened successfully",
            method_used="vi",
            budget_recollected_from_winner=133.2,
            budget_recollected_from_siblings=111.0,
        )

        # Collect all events
        all_events = root.events

        # When: Reconstruct from events
        reconstructed = AgentSession.load_from_history(all_events)

        # Then: State should match
        assert reconstructed.session_id == root_session_id
        assert reconstructed.role == AgentRole.BOSS
        assert abs(reconstructed.current_budget - root.current_budget) < 0.01
        assert len(reconstructed.task_queue) == len(root.task_queue)
        assert len(reconstructed.child_ids) == len(root.child_ids)
        assert reconstructed.child_results.get(winner_id) == root.child_results.get(winner_id)


class TestBudgetCalculations:
    """Test budget calculations from the example."""

    def test_budget_calculation_example(self):
        """Verify the exact budget calculation from the example.

        Initial: 1000
        Allocation: -333 (for subtask 1)
        Sub1 (vim): depleted, 0 returned
        Sub2 (vi): succeeded, 111 * 1.2 = 133.2 returned
        Sub3 (nano): terminated, 111 * 1.0 = 111 returned

        Expected: 1000 - 333 + 0 + 133.2 + 111 = 911.2

        Note: The example says 1022.2 but that assumes Sub3 gets 222 back.
        With equal split (111 each) and the given ratios, we get 911.2.
        If Sub3 had 222 remaining (unequal split), we'd get 1000 - 333 + 133.2 + 222 = 1022.2
        """
        initial_budget = 1000.0
        subtask_allocation = 333.0

        # Equal split scenario: 333 / 3 = 111 per subordinate

        # Sub1: depleted
        sub1_remaining = 0.0
        sub1_recollected = sub1_remaining * 1.0  # No penalty since sibling succeeded

        # Sub2: succeeded
        sub2_remaining = 111.0
        reward_ratio = 1.2
        sub2_recollected = sub2_remaining * reward_ratio

        # Sub3: terminated
        sub3_remaining = 111.0
        neutral_ratio = 1.0
        sub3_recollected = sub3_remaining * neutral_ratio

        # Calculate final budget
        after_allocation = initial_budget - subtask_allocation
        final_budget = after_allocation + sub1_recollected + sub2_recollected + sub3_recollected

        assert abs(final_budget - 911.2) < 0.01

    def test_budget_calculation_with_unequal_split(self):
        """Test budget calculation with unequal budget split (from example text).

        The example text suggests: 1000 - 333 + (111 * 1.2) + 222 = 1022.2
        This implies Sub3 has 222 remaining (not 111), meaning:
        - Sub1 gets 111 (depletes)
        - Sub2 gets 111 (succeeds)
        - Sub3 gets 111, but somehow has 222 remaining?

        Actually re-reading: "the rest of subordinate nodes lose their allocated budget
        with no reward or penalty ratio" means they return 1.0x of remaining.
        If Sub3 was terminated early (didn't use any budget), remaining = 111.

        The 222 might refer to the combined remaining from Sub1 (if it didn't deplete)
        and Sub3. Let's test that scenario.
        """
        initial_budget = 1000.0
        subtask_allocation = 333.0

        # Scenario: Sub1 didn't fully deplete, has 111 remaining
        sub1_remaining = 111.0
        sub2_remaining = 111.0
        sub3_remaining = 111.0

        # Only Sub2 succeeded
        reward_ratio = 1.2
        neutral_ratio = 1.0

        sub2_recollected = sub2_remaining * reward_ratio  # 133.2
        # Siblings get neutral ratio
        sub1_recollected = sub1_remaining * neutral_ratio  # 111
        sub3_recollected = sub3_remaining * neutral_ratio  # 111

        after_allocation = initial_budget - subtask_allocation
        final_budget = after_allocation + sub1_recollected + sub2_recollected + sub3_recollected

        # 667 + 111 + 133.2 + 111 = 1022.2
        assert abs(final_budget - 1022.2) < 0.01
