"""Direct tests for ``infrastructure.workers.claude_code_worker.ClaudeCodeWorker``.

The subprocess invocation is mocked: real ``claude -p`` is never spawned. The
tests pin argv shape, env allowlist enforcement, scratch-settings emission,
and timeout handling — the four behaviors that determine byte-identity with
the legacy baseline runner under the new dispatch path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.application.run_invariants import (
    TaskPromptSpec,
    TimeoutBudget,
    ToolPolicy,
    WorkerResult,
    WorkspaceSpec,
)
from core.domain.exceptions import ToolNotAvailableError
from infrastructure.workers.claude_code_worker import ClaudeCodeWorker


def _make_spec(prompt: str = "task body") -> TaskPromptSpec:
    return TaskPromptSpec(
        rendered_prompt=prompt,
        briefing_sha="0" * 64,
        cve_context=None,
        task="t",
    )


def _make_policy(
    *,
    allowed: tuple[str, ...] = ("Bash",),
    disallowed: tuple[str, ...] = (),
    bash_cmds: tuple[str, ...] = (),
) -> ToolPolicy:
    return ToolPolicy(
        allowed=allowed,
        disallowed=disallowed,
        allowed_bash_commands=bash_cmds,
    )


def _make_workspace(tmp_path: Path) -> WorkspaceSpec:
    return WorkspaceSpec(root=tmp_path, extras={})


def _fake_process(returncode: int = 0) -> AsyncMock:
    """Build a fake asyncio process with a configurable return code.

    ``Process.wait()`` is async; ``kill()`` and ``terminate()`` are sync.
    The mocks reflect that contract.
    """
    proc = AsyncMock()
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    # MagicMock (sync) — Process.kill() is not a coroutine.
    from unittest.mock import MagicMock

    proc.kill = MagicMock()
    return proc


@pytest.fixture
def run_id() -> UUID:
    return uuid4()


def test_run_task_raises_when_claude_not_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """If ``shutil.which("claude")`` returns None, fail fast with a domain error."""
    # Given: no `claude` binary on PATH.
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: None,
    )
    worker = ClaudeCodeWorker()

    # When/Then: invocation raises ToolNotAvailableError (a domain exception).
    with pytest.raises(ToolNotAvailableError):
        asyncio.run(
            worker.run_task(
                run_id=run_id,
                spec=_make_spec(),
                tool_policy=_make_policy(),
                timeouts=TimeoutBudget(per_worker_call=10, per_run_total=20),
                workspace=_make_workspace(tmp_path),
            )
        )


def test_run_task_builds_expected_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """argv mirrors the legacy baseline's flag set and threads --disallowedTools.

    Verifies ``-p``, ``--output-format stream-json``, ``--include-partial-messages``
    and the ``--disallowedTools <comma>`` mapping from ``ToolPolicy.disallowed``.
    """
    # Given: a stubbed `claude` binary path and a captured create_subprocess_exec.
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()
    spec = _make_spec("task body")
    policy = _make_policy(disallowed=("WebFetch", "Browser"))

    # When: running the task.
    result = asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=spec,
            tool_policy=policy,
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: the captured argv has the canonical flag set and ends with the prompt.
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert argv[0] == "/usr/bin/claude"
    assert "-p" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--include-partial-messages" in argv
    # Disallowed tools are joined by comma.
    idx = argv.index("--disallowedTools")
    assert argv[idx + 1] == "WebFetch,Browser"
    # Last argv arg is the prompt.
    assert argv[-1] == "task body"
    # And: result reflects the subprocess return code 0 -> "completed".
    assert isinstance(result, WorkerResult)
    assert result.exit_status == "completed"


def test_run_task_strips_disallowed_flag_when_policy_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """When ``ToolPolicy.disallowed`` is empty, ``--disallowedTools`` is omitted."""
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()

    # When: running with no disallowed tools.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(disallowed=()),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: --disallowedTools is absent from argv.
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert "--disallowedTools" not in argv


def test_run_task_env_strips_secrets_and_injects_scratch_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """The subprocess env is allowlist-only and ``CLAUDE_CONFIG_DIR`` points
    at a freshly-materialized scratch directory."""
    # Given: secrets present in os.environ that must NOT leak through.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret")  # must be stripped
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")  # must be stripped
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )

    captured: dict[str, object] = {}

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()

    # When: running the task.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: the env carries allowlisted keys and excludes secrets.
    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("ANTHROPIC_API_KEY") == "sk-anthropic-test"
    assert "POSTGRES_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    # And: CLAUDE_CONFIG_DIR points at a real directory (cleaned up on exit,
    # so we just assert the key exists during the call by capturing it).
    assert "CLAUDE_CONFIG_DIR" in env
    assert env["CLAUDE_CONFIG_DIR"]


def test_run_task_writes_settings_json_with_bash_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """The scratch ``CLAUDE_CONFIG_DIR`` contains a ``settings.json`` with
    ``permissions.allow = ['Bash(<cmd>)' ...]`` for each bash command."""
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )
    settings_payloads: list[dict[str, object]] = []

    async def _fake_exec(*_args, **kwargs):
        # Capture the settings.json content while the scratch dir still exists.
        env = kwargs.get("env") or {}
        scratch = Path(env["CLAUDE_CONFIG_DIR"])
        settings_path = scratch / "settings.json"
        settings_payloads.append(json.loads(settings_path.read_text(encoding="utf-8")))
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()

    # When: running with a non-empty bash allowlist.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(bash_cmds=("valgrind", "klee")),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: the scratch settings.json mirrors the bash allowlist.
    assert len(settings_payloads) == 1
    payload = settings_payloads[0]
    assert payload == {"permissions": {"allow": ["Bash(valgrind)", "Bash(klee)"]}}


def test_run_task_emits_empty_allowlist_when_policy_has_no_bash_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """Even with no bash commands, ``settings.json`` is emitted with an empty
    allow list — claude must see explicit policy, not its defaults."""
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )
    captured: list[dict[str, object]] = []

    async def _fake_exec(*_args, **kwargs):
        scratch = Path((kwargs.get("env") or {})["CLAUDE_CONFIG_DIR"])
        captured.append(json.loads((scratch / "settings.json").read_text(encoding="utf-8")))
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()

    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(bash_cmds=()),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    assert captured == [{"permissions": {"allow": []}}]


def test_run_task_returns_failed_exit_status_when_subprocess_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """Non-zero subprocess return code is classified as ``failed``."""
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )

    async def _fake_exec(*_args, **_kwargs):
        return _fake_process(returncode=2)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()

    # When: invoking the task with a process that exits 2.
    result = asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: result.exit_status is "failed".
    assert result.exit_status == "failed"


def test_run_task_returns_timeout_exit_status_when_wait_for_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """``asyncio.TimeoutError`` translates to a timeout WorkerResult."""
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )

    proc = _fake_process(returncode=0)

    async def _fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    # Patch wait_for so it consumes (closes) the awaitable it receives and then
    # raises TimeoutError. Closing keeps Python's "coroutine was never awaited"
    # check quiet without requiring real time to pass.
    async def _raise_timeout(coro, *_args, **_kwargs):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.wait_for",
        _raise_timeout,
    )

    worker = ClaudeCodeWorker()

    # When: running with a wait_for that times out.
    result = asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=1, per_run_total=10),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: WorkerResult marks the run as timeout and the process was killed.
    assert result.exit_status == "timeout"
    assert proc.kill.called


def test_run_task_records_log_message_on_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_id: UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker logs an informational message naming the binary and cwd
    so production debugging has breadcrumbs."""
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )

    async def _fake_exec(*_args, **_kwargs):
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()

    with caplog.at_level(logging.INFO, logger="infrastructure.workers.claude_code_worker"):
        asyncio.run(
            worker.run_task(
                run_id=run_id,
                spec=_make_spec(),
                tool_policy=_make_policy(),
                timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
                workspace=_make_workspace(tmp_path),
            )
        )

    assert any("claude" in record.getMessage() for record in caplog.records)
