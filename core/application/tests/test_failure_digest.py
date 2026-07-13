"""Tests for the deterministic failure digest builder."""

from uuid import uuid4

from core.application.services.orchestration.failure_digest import build_failure_digest
from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import ThoughtCaptured


def _config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


def _worker() -> AgentSession:
    worker = AgentSession.create(agent_id=uuid4(), role=AgentRole.WORKER, config=_config())
    worker.assign_task("reproduce the issue")
    return worker


def _capture(
    worker: AgentSession,
    content: str,
    output_type: str = "tool_result",
    tool_name: str | None = "bash",
) -> None:
    """Populate recent_thoughts via the canonical ThoughtCaptured apply path."""
    worker.apply_worker_event(
        ThoughtCaptured(
            aggregate_id=worker.agent_id,
            sequence_number=0,
            content=content,
            output_type=output_type,
            tool_name=tool_name,
        )
    )


def test_digest_exact_format_with_thoughts() -> None:
    """The digest renders FAILURE, per-thought lines oldest-first, then ATTEMPT."""

    # Given: a crashed worker that captured two tool outputs in order
    worker = _worker()
    _capture(worker, "ran make", tool_name="bash")
    _capture(worker, "applied patch", tool_name="edit")
    worker.fail_with_reason("Segfault in parser")

    # When: the digest is built
    digest = build_failure_digest(worker)

    # Then: the layout matches the spec byte-for-byte (most recent thought last)
    assert digest == (
        "FAILURE: Segfault in parser\n"
        "LAST TOOL CALLS:\n"
        "[bash/tool_result] ran make\n"
        "[edit/tool_result] applied patch\n"
        "ATTEMPT: 1"
    )


def test_digest_orders_thoughts_oldest_to_newest() -> None:
    """recent_thoughts render oldest-first, with the most recent immediately
    above the ATTEMPT footer."""

    # Given: a worker that captured three ordered outputs
    worker = _worker()
    _capture(worker, "first")
    _capture(worker, "second")
    _capture(worker, "third")
    worker.fail_with_reason("boom")

    # When: the digest is built
    lines = build_failure_digest(worker).splitlines()

    # Then: the call lines appear oldest → newest
    assert lines.index("[bash/tool_result] first") < lines.index("[bash/tool_result] third")
    # And: the newest call is the last line before ATTEMPT
    assert lines[-2] == "[bash/tool_result] third"
    assert lines[-1] == "ATTEMPT: 1"


def test_digest_empty_thoughts_placeholder() -> None:
    """With no captured tool output, a single placeholder line is emitted."""

    # Given: a crashed worker that captured nothing
    worker = _worker()
    worker.fail_with_reason("adapter returned no output")

    # When: the digest is built
    digest = build_failure_digest(worker)

    # Then: the placeholder stands in for the tool-call tail
    assert digest == (
        "FAILURE: adapter returned no output\n"
        "LAST TOOL CALLS:\n"
        "(no tool output captured)\n"
        "ATTEMPT: 1"
    )


def test_digest_renders_missing_tool_name_as_dash() -> None:
    """A thought without a tool name renders '-' in the tool slot."""

    # Given: a worker whose captured excerpt has no tool name
    worker = _worker()
    _capture(worker, "thinking about the crash", output_type="reasoning", tool_name=None)
    worker.fail_with_reason("boom")

    # When/Then: the missing tool name becomes '-'
    assert "[-/reasoning] thinking about the crash" in build_failure_digest(worker)


def test_digest_defaults_unknown_error() -> None:
    """A missing error message falls back to 'unknown'."""

    # Given: a worker with no recorded failure reason
    worker = _worker()

    # When/Then: the FAILURE line defaults to 'unknown'
    assert build_failure_digest(worker).startswith("FAILURE: unknown\n")


def test_digest_reflects_retry_count_in_attempt() -> None:
    """ATTEMPT reports retry_count + 1 so the first attempt reads ATTEMPT: 1."""

    # Given: a worker that already failed and retried once
    worker = _worker()
    worker.fail_with_reason("first crash")
    worker.schedule_retry(reason="first crash")
    worker.fail_with_reason("second crash")

    # When/Then: ATTEMPT reflects the second attempt
    assert build_failure_digest(worker).endswith("ATTEMPT: 2")


def test_digest_is_deterministic_and_replay_stable() -> None:
    """The same aggregate state always yields an identical digest."""

    # Given: a crashed worker with captured output
    worker = _worker()
    _capture(worker, "step one")
    _capture(worker, "step two")
    worker.fail_with_reason("nondeterminism check")

    # When: the digest is built twice, and once from a replay of the same events
    replayed = AgentSession.load_from_history(list(worker.events))

    # Then: all three strings are identical
    assert build_failure_digest(worker) == build_failure_digest(worker)
    assert build_failure_digest(replayed) == build_failure_digest(worker)


def test_digest_caps_at_4000_chars_with_omission_marker() -> None:
    """An oversized transcript is head+tail truncated with an omission marker."""

    # Given: a worker with 20 near-max excerpts (transcript far exceeds 4000 chars)
    worker = _worker()
    for i in range(20):
        _capture(worker, f"chunk-{i} " + "x" * 480)
    worker.fail_with_reason("oversized transcript")

    # When: the digest is built
    digest = build_failure_digest(worker)

    # Then: the whole string is bounded near the 4000-char cap
    assert len(digest) < 4100
    # And: the omission marker is present and both ends survive
    assert "chars omitted" in digest
    assert digest.startswith("FAILURE: oversized transcript")
    assert digest.rstrip().endswith("ATTEMPT: 1")
