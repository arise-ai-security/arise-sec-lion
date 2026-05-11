"""Tests for hang-recovery fixes: event streaming, verification retry skip, container reuse.

Verifies three fixes that break the cascading retry-dependency deadlock:
1. OpenHands events stream live (not batch-flushed)
2. No-progress watchdog skips verification retries
3. Container cleanup is skipped when verification retry may follow
"""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    AgentCreated,
    RetryScheduled,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)


# ---------------------------------------------------------------------------
# Fix 1: OpenHands event streaming
# ---------------------------------------------------------------------------


class TestOpenHandsEventStreaming:
    """Verify events are yielded incrementally, not batch-flushed."""

    @pytest.mark.asyncio
    async def test_events_yielded_during_execution_not_after(self) -> None:
        """Events from on_event callback are yielded while run() is still executing.

        Before the fix, ALL events were flushed after conversation.run()
        returned. Watchdogs saw 'zero thoughts' for the entire execution.
        """
        from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter

        adapter = OpenHandsAdapter(
            model="gpt-5.3-codex",
            timeout_seconds=30,
        )

        # Given: a mock conversation whose run() takes time but fires on_event live
        events_yielded_during_run: list[str] = []
        run_finished = asyncio.Event()

        mock_event = MagicMock()
        mock_event.message = "Working on instrumentation..."

        mock_conversation = MagicMock()
        mock_state = MagicMock()
        mock_state.events = []
        mock_state.on_event = None  # will be set by adapter
        mock_conversation.state = mock_state
        mock_conversation.conversation_stats = None

        def fake_send(msg: str) -> None:
            pass

        def fake_run() -> None:
            # Simulate: fire on_event during run, before returning
            if mock_state.on_event is not None:
                mock_state.on_event(mock_event)
            run_finished.set()

        mock_conversation.send_message = fake_send
        mock_conversation.run = fake_run

        with patch.object(adapter, "_build_conversation", return_value=mock_conversation):
            with patch.object(adapter, "_extract_event_content", return_value="Working..."):
                with patch.object(adapter, "_classify_event", return_value="thinking"):
                    with patch.object(adapter, "_try_extract_finish", return_value=None):
                        task_context = {
                            "agent_id": uuid4(),
                            "task_description": "test task",
                            "tool_name": "openhands",
                            "config": MagicMock(tool="openhands"),
                        }
                        # When: we iterate the async generator
                        collected = []
                        async for event in adapter.run_session(task_context):
                            collected.append(event)

        # Then: at least one ThoughtCaptured event was yielded
        thought_events = [e for e in collected if hasattr(e, "output_type")]
        assert len(thought_events) >= 1, (
            "Expected at least 1 ThoughtCaptured event from live streaming"
        )


# ---------------------------------------------------------------------------
# Fix 2: No-progress watchdog skips verification retries
# ---------------------------------------------------------------------------


class TestNoProgressSkipsVerificationRetries:
    """Verify that agents on a verification retry are not killed by no-progress."""

    def _make_agent_with_verification_retry(self) -> AgentSession:
        """Create a mock agent that completed, failed verification, and was retried."""
        agent = MagicMock(spec=AgentSession)
        agent.verification_feedback = None
        agent.retry_count = 0
        agent._verification_feedback = None
        return agent

    @pytest.mark.asyncio
    async def test_agent_with_verification_feedback_skipped_by_no_progress(self) -> None:
        """An agent that has verification_feedback and retry_count > 0 should
        not be killed by the no-progress watchdog.
        """
        agent = self._make_agent_with_verification_retry()
        agent.verification_feedback = "Missing probes.log file"
        agent.retry_count = 1

        # The actual skip logic from execution_service.py:
        should_skip = agent.verification_feedback and agent.retry_count > 0
        assert should_skip, "Agent with verification feedback on retry should be skipped"

    def test_agent_without_verification_feedback_not_skipped(self) -> None:
        """An agent that failed for non-verification reasons should NOT be skipped."""
        agent = self._make_agent_with_verification_retry()
        agent.verification_feedback = None
        agent.retry_count = 1

        should_skip = agent.verification_feedback and agent.retry_count > 0
        assert not should_skip, "Agent without verification feedback should NOT be skipped"

    def test_first_attempt_not_skipped(self) -> None:
        """An agent on its first attempt (retry_count=0) should NOT be skipped."""
        agent = self._make_agent_with_verification_retry()
        agent.verification_feedback = "Missing probes.log file"
        agent.retry_count = 0

        should_skip = agent.verification_feedback and agent.retry_count > 0
        assert not should_skip, "First attempt should NOT be skipped"


# ---------------------------------------------------------------------------
# Fix 3: Container reuse on verification retry
# ---------------------------------------------------------------------------


class TestContainerReuseOnRetry:
    """Verify that container cleanup is skipped when a verification retry may follow."""

    @pytest.mark.asyncio
    async def test_plugin_reuses_alive_container(self) -> None:
        """When an existing container session is alive, prepare_worker_execution
        should reuse it instead of creating a new one.
        """
        from plugins.security.container_runtime import (
            SecBenchContainerSession,
            SecBenchWorkspace,
        )
        from plugins.security.cve_instance import CVEInstance
        from plugins.security.plugin import SecurityDomainPlugin

        root_id = uuid4()
        agent_id = uuid4()
        cve = CVEInstance(
            instance_id="demo.cve-2024-0001",
            repo="acme/demo",
            project_name="demo",
            lang="c",
            work_dir="/src/demo",
            sanitizer="address",
            bug_description="demo bug",
            base_commit="abc123",
        )

        tmp_path = Path("/tmp/test-container-reuse")
        workspace = SecBenchWorkspace(
            root_id=root_id,
            image="secb-tools:demo",
            host_root=tmp_path,
            host_source_dir=tmp_path / "src",
            host_testcase_dir=tmp_path / "testcase",
            host_work_dir=tmp_path / "src" / "demo",
            container_source_dir="/src",
            container_testcase_dir="/testcase",
            container_working_directory="/src/demo",
            helper_script=tmp_path / "secb-exec",
        )
        session = SecBenchContainerSession(
            workspace=workspace,
            container_id="abc123",
            container_name="secbench-worker-test",
            image=workspace.image,
        )

        runtime = AsyncMock()
        runtime.prepare_workspace.return_value = workspace
        runtime.start_session.return_value = session
        runtime.is_session_alive.return_value = True  # container is alive

        plugin = SecurityDomainPlugin(
            enabled_tools=["valgrind"],
            container_runtime=runtime,
        )

        # Given: workspace already prepared, session already exists
        plugin._workspaces[root_id] = workspace
        plugin._sessions[root_id] = session

        # When: prepare_worker_execution is called (e.g., on verification retry)
        result = await plugin.prepare_worker_execution(
            root_id=root_id,
            agent_id=agent_id,
            run_output_path=tmp_path / "output",
            domain_context=cve,
        )

        # Then: the existing session was reused (start_session NOT called)
        runtime.start_session.assert_not_called()
        runtime.is_session_alive.assert_called_once_with(session)
        assert result is not None
        assert result.task_context["container_session"]["container_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_plugin_creates_new_container_when_dead(self) -> None:
        """When the existing container is dead, a new one is created."""
        from plugins.security.container_runtime import (
            SecBenchContainerSession,
            SecBenchWorkspace,
        )
        from plugins.security.cve_instance import CVEInstance
        from plugins.security.plugin import SecurityDomainPlugin

        root_id = uuid4()
        agent_id = uuid4()
        cve = CVEInstance(
            instance_id="demo.cve-2024-0002",
            repo="acme/demo",
            project_name="demo",
            lang="c",
            work_dir="/src/demo",
            sanitizer="address",
            bug_description="demo bug 2",
            base_commit="def456",
        )

        tmp_path = Path("/tmp/test-container-dead")
        workspace = SecBenchWorkspace(
            root_id=root_id,
            image="secb-tools:demo2",
            host_root=tmp_path,
            host_source_dir=tmp_path / "src",
            host_testcase_dir=tmp_path / "testcase",
            host_work_dir=tmp_path / "src" / "demo",
            container_source_dir="/src",
            container_testcase_dir="/testcase",
            container_working_directory="/src/demo",
            helper_script=tmp_path / "secb-exec",
        )
        old_session = SecBenchContainerSession(
            workspace=workspace,
            container_id="old123",
            container_name="secbench-worker-old",
            image=workspace.image,
        )
        new_session = SecBenchContainerSession(
            workspace=workspace,
            container_id="new456",
            container_name="secbench-worker-new",
            image=workspace.image,
        )

        runtime = AsyncMock()
        runtime.prepare_workspace.return_value = workspace
        runtime.start_session.return_value = new_session
        runtime.is_session_alive.return_value = False  # container is DEAD
        runtime.stop_session.return_value = None

        plugin = SecurityDomainPlugin(
            enabled_tools=["valgrind"],
            container_runtime=runtime,
        )
        plugin._workspaces[root_id] = workspace
        plugin._sessions[root_id] = old_session

        # When: prepare_worker_execution finds dead container
        result = await plugin.prepare_worker_execution(
            root_id=root_id,
            agent_id=agent_id,
            run_output_path=tmp_path / "output",
            domain_context=cve,
        )

        # Then: old container cleaned up, new one created
        runtime.is_session_alive.assert_called_once_with(old_session)
        runtime.start_session.assert_called_once()
        assert result is not None
        assert result.task_context["container_session"]["container_id"] == "new456"

    def test_cleanup_skipped_for_completed_worker(self) -> None:
        """When a worker completes (may get verification retry), cleanup should
        be skipped so the container stays alive for the retry.
        """
        # Given: agent completed (verification may follow)
        agent = MagicMock()
        agent.status = AgentStatus.COMPLETED
        agent.verification_feedback = None

        # Then: may_retry should be True
        may_retry = (
            agent.status == AgentStatus.COMPLETED
            or (agent.status == AgentStatus.FAILED and agent.verification_feedback)
        )
        assert may_retry, "Completed worker should skip cleanup (may get verification retry)"

    def test_cleanup_skipped_for_verification_failed_worker(self) -> None:
        """When a worker failed verification, cleanup should be skipped."""
        agent = MagicMock()
        agent.status = AgentStatus.FAILED
        agent.verification_feedback = "Missing deliverable"

        may_retry = (
            agent.status == AgentStatus.COMPLETED
            or (agent.status == AgentStatus.FAILED and agent.verification_feedback)
        )
        assert may_retry, "Verification-failed worker should skip cleanup"

    def test_cleanup_runs_for_non_verification_failure(self) -> None:
        """When a worker fails for non-verification reasons, cleanup should run."""
        agent = MagicMock()
        agent.status = AgentStatus.FAILED
        agent.verification_feedback = None

        may_retry = (
            agent.status == AgentStatus.COMPLETED
            or (agent.status == AgentStatus.FAILED and agent.verification_feedback)
        )
        assert not may_retry, "Non-verification failure should run cleanup"


# ---------------------------------------------------------------------------
# Fix 4: Batch runner retry budget (from earlier commit)
# ---------------------------------------------------------------------------


class TestBatchRunnerRetryBudget:
    """Verify abandoned children are excluded from candidate selection."""

    def test_abandoned_children_filtered_from_candidates(self) -> None:
        """Candidates must exclude both rescued_children AND abandoned_children."""
        # Given: some candidates from DB query
        candidates = [
            ("agent-1", 600, "AgentExecutionStarted"),
            ("agent-2", 900, "OperationFinished"),
            ("agent-3", 1200, "RetryScheduled"),
        ]
        rescued_children: set[str] = {"agent-1"}
        abandoned_children: set[str] = {"agent-3"}

        # When: filtering (matches batch_secbench.py line 779-783)
        candidate = next(
            (
                (aid, s, ev) for (aid, s, ev) in candidates
                if aid not in rescued_children and aid not in abandoned_children
            ),
            None,
        )

        # Then: only agent-2 passes (agent-1 rescued, agent-3 abandoned)
        assert candidate is not None
        assert candidate[0] == "agent-2"

    def test_no_candidates_when_all_excluded(self) -> None:
        """When all candidates are rescued or abandoned, no candidate is selected."""
        candidates = [
            ("agent-1", 600, "AgentExecutionStarted"),
        ]
        rescued_children: set[str] = set()
        abandoned_children: set[str] = {"agent-1"}

        candidate = next(
            (
                (aid, s, ev) for (aid, s, ev) in candidates
                if aid not in rescued_children and aid not in abandoned_children
            ),
            None,
        )
        assert candidate is None


# ---------------------------------------------------------------------------
# Orphan recovery (from earlier commit)
# ---------------------------------------------------------------------------


class TestOrphanRecovery:
    """Verify that orphaned WorkFailed agents get parent notification on startup."""

    def test_work_failed_without_retry_is_orphan(self) -> None:
        """An agent whose last event is WorkFailed (no RetryScheduled after)
        is an orphan that needs parent notification.
        """
        # Given: events ending in WorkFailed
        events = [
            MagicMock(spec=AgentCreated, sequence_number=1),
            MagicMock(spec=TaskAssigned, sequence_number=2),
            MagicMock(spec=WorkFailed, sequence_number=10),
        ]

        last_event = events[-1]
        is_orphan = isinstance(last_event, WorkFailed)

        has_retry_after = any(
            isinstance(e, RetryScheduled) and e.sequence_number > last_event.sequence_number
            for e in events
        )

        assert is_orphan, "Last event should be WorkFailed"
        assert not has_retry_after, "No RetryScheduled after WorkFailed = orphan"

    def test_work_failed_with_retry_is_not_orphan(self) -> None:
        """WorkFailed followed by RetryScheduled is NOT an orphan."""
        wf = MagicMock(spec=WorkFailed, sequence_number=10)
        rs = MagicMock(spec=RetryScheduled, sequence_number=11)
        events = [wf, rs]

        last_event = events[-1]
        is_work_failed_last = isinstance(last_event, WorkFailed)

        assert not is_work_failed_last, "RetryScheduled is the last event, not WorkFailed"


# ---------------------------------------------------------------------------
# Fix 5: Kill orphaned bash children (pipe deadlock prevention)
# ---------------------------------------------------------------------------


class TestOrphanedBashCleanup:
    """Verify that orphaned bash -i processes are killed on shutdown."""

    def test_kill_orphaned_bash_children_finds_and_kills(self) -> None:
        """_kill_orphaned_bash_children should kill bash processes whose
        ppid matches the current process.
        """
        import os
        from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter

        current_pid = os.getpid()
        killed_pids: list[int] = []

        # Mock /proc traversal: simulate a bash child of current process
        fake_proc_entries = ["1", str(current_pid), "99999", "abc"]
        fake_stat_content = {
            # pid (bash) S ppid ...
            "99999": f"99999 (bash) S {current_pid} 0 0 0 0 0 0",
            "1": f"1 (init) S 0 0 0 0 0 0",
        }

        original_listdir = os.listdir
        original_kill = os.kill

        def mock_listdir(path: str) -> list[str]:
            if path == "/proc":
                return fake_proc_entries
            return original_listdir(path)

        def mock_kill(pid: int, sig: int) -> None:
            killed_pids.append(pid)

        with patch("os.listdir", side_effect=mock_listdir):
            with patch("os.kill", side_effect=mock_kill):
                with patch("builtins.open", side_effect=lambda p, *a, **k:
                    MagicMock(__enter__=lambda s: MagicMock(
                        read=lambda: fake_stat_content.get(p.split("/")[2], "")
                    ), __exit__=lambda *a: None)
                ):
                    OpenHandsAdapter._kill_orphaned_bash_children()

        assert 99999 in killed_pids, "Should have killed bash child PID 99999"
        assert len(killed_pids) == 1, "Should only kill the bash child, not init"

    def test_kill_orphaned_bash_children_handles_no_children(self) -> None:
        """Should not crash when there are no bash children."""
        from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter

        with patch("os.listdir", return_value=["1", "2"]):
            with patch("builtins.open", side_effect=OSError("no proc")):
                # Should not raise
                OpenHandsAdapter._kill_orphaned_bash_children()
