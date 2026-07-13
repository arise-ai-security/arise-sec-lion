"""Prompt plumbing for failure-context propagation.

Covers two seams:
- The decomposition prompt's ``<previous_attempt_failures>`` block, rendered
  from ``failure_history`` in the uncached tail (``decomposition_variable.j2``).
- The worker retry injection assembled by
  ``AgentOrchestrator._build_retry_context_block`` (verification feedback and/or
  failure digest).
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.services import PromptBuilder
from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.values.failure import ChildFailureRecord


def _prompts_dir() -> Path:
    docker_path = Path("/app/prompts")
    if docker_path.exists():
        return docker_path
    local_path = Path(__file__).parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    return Path("prompts")


def _builder() -> PromptBuilder:
    return PromptBuilder(template_dir=_prompts_dir(), default_tool="claude_code")


def _worker_config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 1000},
        "tool": "claude_code",
    }


# ===== Decomposition prompt: <previous_attempt_failures> block =====


class TestDecompositionFailureHistoryBlock:
    """failure_history renders per-child task, reason, and bounded digest."""

    def test_non_empty_history_renders_labels_reason_and_digest_excerpt(self) -> None:
        # Given: a manager whose sole prior child crashed, with an oversized digest.
        builder = _builder()
        oversized_digest = "HEAD_MARKER_" + ("x" * 2000) + "_TAIL_MARKER"
        history = (
            ChildFailureRecord(
                child_id=uuid4(),
                child_task="Reproduce the reported failure with the fixture",
                reason="worker stopped before producing artifact.bin",
                digest=oversized_digest,
            ),
        )

        # When: building the manager decomposition prompt with that history.
        prompt = builder.build_manager_decomposition_prompt(
            task_description="[Analysis] decompose the reproduction phase",
            agent_id=uuid4(),
            failure_history=history,
        )

        # Then: the guarded block renders with the subtask label and reason.
        assert "<previous_attempt_failures>" in prompt
        assert "Reproduce the reported failure with the fixture" in prompt
        assert "worker stopped before producing artifact.bin" in prompt

        # And: the oversized digest is excerpted head + tail with the omission marker.
        assert "HEAD_MARKER_" in prompt
        assert "_TAIL_MARKER" in prompt
        assert "[...omitted...]" in prompt

    def test_task_and_reason_are_bounded(self) -> None:
        # Given: a child record whose task and reason exceed the render bounds.
        builder = _builder()
        history = (
            ChildFailureRecord(
                child_id=uuid4(),
                child_task="T" * 400,
                reason="R" * 900,
                digest=None,
            ),
        )

        # When: building the prompt.
        prompt = builder.build_manager_decomposition_prompt(
            task_description="[Analysis] decompose",
            agent_id=uuid4(),
            failure_history=history,
        )

        # Then: task is capped at 200 chars and reason at 500.
        assert "T" * 200 in prompt
        assert "T" * 201 not in prompt
        assert "R" * 500 in prompt
        assert "R" * 501 not in prompt

    def test_empty_history_omits_block(self) -> None:
        # Given: a manager with no failure history.
        builder = _builder()

        # When: building the prompt without failure_history.
        prompt = builder.build_manager_decomposition_prompt(
            task_description="[Analysis] decompose",
            agent_id=uuid4(),
        )

        # Then: the guarded block is absent entirely.
        assert "<previous_attempt_failures>" not in prompt

    def test_boss_decomposition_also_renders_block(self) -> None:
        # Given: a boss whose prior decomposition wholly failed (shared template).
        builder = _builder()
        history = (
            ChildFailureRecord(
                child_id=uuid4(),
                child_task="Phase 1: build a reproducing harness",
                reason="all children failed",
                digest=None,
            ),
        )

        # When: building the boss delegation prompt with that history.
        prompt = builder.build_boss_delegation_prompt(
            task_description="Resolve the issue",
            agent_id=uuid4(),
            failure_history=history,
        )

        # Then: the same guarded block renders (boss uses decomposition_variable.j2).
        assert "<previous_attempt_failures>" in prompt
        assert "Phase 1: build a reproducing harness" in prompt


# ===== Worker retry injection: _build_retry_context_block =====


@dataclass(frozen=True)
class FakeRetryAgent:
    """Minimal stand-in exposing the fields the retry-context builder reads."""

    verification_feedback: str | None = None
    success_criteria: str = ""
    failure_digest: str | None = None


class TestBuildRetryContextBlock:
    """Feedback and digest render as independent blocks, feedback first."""

    def test_digest_only_has_failure_block_not_feedback_block(self) -> None:
        # Given: a retry agent with a failure digest but no verifier feedback.
        agent = FakeRetryAgent(
            failure_digest="FAILURE: process stopped\nLAST TOOL CALLS: run artifact.bin\nATTEMPT: 2",
        )

        # When: assembling the retry context.
        block = AgentOrchestrator._build_retry_context_block(agent)

        # Then: the failure block is present with the digest verbatim...
        assert "## Previous Attempt Failure (Retry)" in block
        assert "FAILURE: process stopped\nLAST TOOL CALLS: run artifact.bin\nATTEMPT: 2" in block

        # And: the verification-feedback block is absent.
        assert "## Previous Attempt Feedback (Retry)" not in block

    def test_feedback_only_has_feedback_block_not_failure_block(self) -> None:
        # Given: a retry agent with verifier feedback but no digest.
        agent = FakeRetryAgent(
            verification_feedback="artifact.bin is missing from the workspace",
            success_criteria="artifact.bin exists and triggers validation",
        )

        # When: assembling the retry context.
        block = AgentOrchestrator._build_retry_context_block(agent)

        # Then: the feedback block (with success criteria) is present...
        assert "## Previous Attempt Feedback (Retry)" in block
        assert "artifact.bin is missing from the workspace" in block
        assert "artifact.bin exists and triggers validation" in block

        # And: the failure block is absent.
        assert "## Previous Attempt Failure (Retry)" not in block

    def test_both_present_feedback_precedes_digest(self) -> None:
        # Given: a retry agent carrying both signals.
        agent = FakeRetryAgent(
            verification_feedback="output did not match expected",
            failure_digest="FAILURE: assertion error",
        )

        # When: assembling the retry context.
        block = AgentOrchestrator._build_retry_context_block(agent)

        # Then: both blocks render, feedback before digest.
        feedback_at = block.index("## Previous Attempt Feedback (Retry)")
        failure_at = block.index("## Previous Attempt Failure (Retry)")
        assert feedback_at < failure_at

    def test_neither_present_returns_empty(self) -> None:
        # Given: a retry agent with no failure context.
        agent = FakeRetryAgent()

        # When/Then: the assembled block is empty (prompt stays unchanged).
        assert AgentOrchestrator._build_retry_context_block(agent) == ""

    def test_real_agent_digest_survives_retry_and_renders(self) -> None:
        # Given: a real worker agent that failed, recorded a digest, then retried.
        agent = AgentSession.create(
            agent_id=uuid4(),
            role=AgentRole.WORKER,
            config=_worker_config(),
        )
        agent.assign_task("[Execution] apply the requested change")
        agent.fail_with_reason("worker crashed")
        agent.record_failure_digest("FAILURE: null deref\nATTEMPT: 1", source="worker_crash")
        agent.schedule_retry(reason="worker crashed")

        # When: the agent is on a retry with the digest retained.
        assert agent.retry_count == 1
        assert agent.verification_feedback is None
        block = AgentOrchestrator._build_retry_context_block(agent)

        # Then: the digest block renders from the real, retained field.
        assert "## Previous Attempt Failure (Retry)" in block
        assert "FAILURE: null deref\nATTEMPT: 1" in block
