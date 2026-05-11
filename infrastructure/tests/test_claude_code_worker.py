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
        prompt_sha="0" * 64,
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
    and one ``--disallowedTools`` flag per entry in ``ToolPolicy.disallowed``.
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
    # Disallowed tools are emitted one --disallowedTools flag per name,
    # matching the legacy baseline runner's per-arg shape.
    assert argv.count("--disallowedTools") == 2
    assert argv.count("WebFetch") == 1
    assert argv.count("Browser") == 1
    # Last argv arg is the prompt.
    assert argv[-1] == "task body"
    # And: result reflects the subprocess return code 0 -> "completed".
    assert isinstance(result, WorkerResult)
    assert result.exit_status == "completed"


def test_run_task_threads_model_max_turns_and_allowed_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """Global worker config is reflected in the Claude Code CLI invocation."""
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

    worker = ClaudeCodeWorker(model="claude-sonnet-4-6", max_turns=40)

    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(allowed=("Read", "Bash")),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    model_idx = argv.index("--model")
    assert list(argv[model_idx : model_idx + 2]) == ["--model", "claude-sonnet-4-6"]
    turns_idx = argv.index("--max-turns")
    assert list(argv[turns_idx : turns_idx + 2]) == ["--max-turns", "40"]
    assert argv.count("--allowedTools") == 2
    assert "Read" in argv
    assert "Bash" in argv


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


def test_run_task_emits_one_disallowed_flag_per_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """argv contains one ['--disallowedTools', name] pair per disallowed tool,
    matching the legacy baseline runner's shape."""
    # Given: a stubbed `claude` binary path and a captured create_subprocess_exec.
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
    policy = _make_policy(disallowed=("Task", "Foo"))

    # When: capture argv with two disallowed tools.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=policy,
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: argv contains "--disallowedTools" twice, "Task" once, "Foo" once.
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert argv.count("--disallowedTools") == 2
    assert argv.count("Task") == 1
    assert argv.count("Foo") == 1
    # And: each --disallowedTools flag is immediately followed by a tool name
    # (no comma-joined fallback).
    indices = [i for i, arg in enumerate(argv) if arg == "--disallowedTools"]
    followers = [argv[i + 1] for i in indices]
    assert sorted(followers) == ["Foo", "Task"]


def test_run_task_emits_single_disallowed_flag_when_policy_has_one_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """A single-tool exclusion (the A2 case today) emits one --disallowedTools pair."""
    # Given: a stubbed `claude` binary path and a captured create_subprocess_exec.
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
    policy = _make_policy(disallowed=("Task",))

    # When: running with exactly one disallowed tool.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=policy,
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    # Then: argv has exactly one --disallowedTools flag followed by "Task".
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert argv.count("--disallowedTools") == 1
    idx = argv.index("--disallowedTools")
    assert argv[idx + 1] == "Task"


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


def test_run_task_can_use_global_claude_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """A-cells may use the operator/global Claude config while preserving CLI policy."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: "/usr/bin/claude",
    )
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = kwargs.get("env")
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker(use_global_config=True)

    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(disallowed=("Task",)),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert "CLAUDE_CONFIG_DIR" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-anthropic-test"

    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert "--disallowedTools" in argv
    assert argv[argv.index("--disallowedTools") + 1] == "Task"


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


def test_run_task_writes_mcp_config_and_threads_flag_when_extras_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """When ``workspace.extras['mcp_servers']`` is set, the worker writes
    ``mcp_servers.json`` and threads ``--mcp-config <abs path>`` into argv."""
    # Given: a stubbed `claude` binary path and captured argv.
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
    workspace = WorkspaceSpec(
        root=tmp_path,
        extras={
            "mcp_servers": {
                "security_tools": {
                    "command": "python",
                    "args": ["-m", "plugins.security.mcp.security_tools_server"],
                    "env": {
                        "ARISE_SECBENCH_CONTAINER_ID": "abc123",
                        "ARISE_SECBENCH_HELPER_SCRIPT": "/run/secb-exec",
                    },
                }
            }
        },
    )

    # When: running the task with extras carrying an MCP server spec.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )

    # Then: argv contains --mcp-config <abs path>.
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert "--mcp-config" in argv
    idx = argv.index("--mcp-config")
    config_path = Path(argv[idx + 1])
    assert config_path.is_absolute()
    assert config_path.name == "mcp_servers.json"
    # And: the JSON file is on disk wrapped in the camelCase envelope.
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mcpServers" in payload
    assert "security_tools" in payload["mcpServers"]
    server_spec = payload["mcpServers"]["security_tools"]
    assert server_spec["command"] == "python"
    assert server_spec["env"]["ARISE_SECBENCH_CONTAINER_ID"] == "abc123"


def test_run_task_omits_mcp_config_when_extras_lacks_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """Without ``workspace.extras['mcp_servers']``, ``--mcp-config`` is absent."""
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

    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=_make_workspace(tmp_path),
        )
    )

    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert "--mcp-config" not in argv
    # And: no mcp_servers.json on disk in the run dir.
    assert not (tmp_path / "mcp_servers.json").exists()


def _container_session_extras(tmp_path: Path) -> dict[str, object]:
    """Build a ``workspace.extras`` payload that selects the docker-exec path."""
    return {
        "container_session": {
            "container_id": "abc123def456",
            "container_name": "secbench-worker-demo",
            "image": "secb-tools:demo",
            "workspace_root": str(tmp_path),
            "host_source_dir": str(tmp_path / "src"),
            "host_testcase_dir": str(tmp_path / "testcase"),
            "host_work_dir": str(tmp_path / "src" / "demo"),
            "container_source_dir": "/src",
            "container_testcase_dir": "/testcase",
            "container_working_directory": "/src/demo",
            "container_workspace_root": "/arise-run",
            "helper_script": str(tmp_path / "secb-exec"),
        }
    }


def test_run_task_uses_docker_exec_when_container_session_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """When ``workspace.extras`` carries a container session, the worker
    invokes ``claude`` inside the container via ``docker exec``."""
    # Given: a container session is plumbed through workspace.extras.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = kwargs.get("env")
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()
    workspace = WorkspaceSpec(root=tmp_path, extras=_container_session_extras(tmp_path))

    # When: running the task.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec("task body"),
            tool_policy=_make_policy(disallowed=("Task",)),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )

    # Then: argv begins with ``docker exec -i -w <work_dir>`` and ``claude``
    # runs inside the container (binary name only, no host PATH lookup).
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert argv[0] == "docker"
    assert argv[1] == "exec"
    assert "-i" in argv[:5]
    work_idx = argv.index("-w")
    assert argv[work_idx + 1] == "/src/demo"
    # Container id appears immediately before the ``claude`` argv.
    claude_idx = argv.index("claude")
    assert argv[claude_idx - 1] == "abc123def456"
    # And: argv ends with the prompt body (no host/container prefix).
    assert argv[-1] == "task body"
    # And: --disallowedTools Task survives across the wrapper.
    assert "Task" in argv


def test_run_task_in_container_does_not_probe_host_path_for_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """In containerized mode, ``shutil.which("claude")`` is never consulted —
    the binary lives inside the secb-tools image."""
    # Given: shutil.which would return None (no host claude); env is set.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.shutil.which",
        lambda _: None,
    )

    async def _fake_exec(*_args, **_kwargs):
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()
    workspace = WorkspaceSpec(root=tmp_path, extras=_container_session_extras(tmp_path))

    # When/Then: invocation does not raise ToolNotAvailableError.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )


def test_run_task_in_container_emits_e_flags_for_narrow_env_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """Env vars are passed via ``-e KEY=VAL`` flags; host PATH/HOME/USER are
    NOT forwarded so the container's base env is preserved."""
    # Given: host env that includes things which must NOT leak into the container.
    monkeypatch.setenv("PATH", "/host/usr/bin")
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("USER", "operator")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")  # must be stripped
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret")  # must be stripped

    captured: dict[str, object] = {}

    async def _fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()
    workspace = WorkspaceSpec(root=tmp_path, extras=_container_session_extras(tmp_path))

    # When: running the task.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )

    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    e_flags = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    # Then: ANTHROPIC_API_KEY is forwarded via -e, host PATH/HOME/USER are NOT,
    # and CLAUDE_CONFIG_DIR is remapped onto the container workspace mount.
    forwarded_keys = {flag.split("=", 1)[0] for flag in e_flags}
    assert "ANTHROPIC_API_KEY" in forwarded_keys
    assert "PATH" not in forwarded_keys
    assert "HOME" not in forwarded_keys
    assert "USER" not in forwarded_keys
    assert "OPENAI_API_KEY" not in forwarded_keys
    assert "POSTGRES_PASSWORD" not in forwarded_keys
    config_flag = next((flag for flag in e_flags if flag.startswith("CLAUDE_CONFIG_DIR=")), None)
    assert config_flag is not None
    assert config_flag.startswith("CLAUDE_CONFIG_DIR=/arise-run/")


def test_run_task_in_container_drops_host_container_prompt_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """In containerized mode the agent IS inside the container, so the
    host/container duality prompt prefix must NOT be appended."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()
    workspace = WorkspaceSpec(root=tmp_path, extras=_container_session_extras(tmp_path))

    # When: running with a known prompt body.
    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec("solve the bug"),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )

    # Then: argv ends with the raw prompt — no "## Container-backed workspace"
    # / "ABSOLUTE RULE — TWO SEPARATE FILESYSTEMS" prefix.
    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert argv[-1] == "solve the bug"


def test_run_task_in_container_rewrites_mcp_config_for_in_container_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_id: UUID,
) -> None:
    """In containerized mode the host-side stdio config is rewritten so the
    MCP server runs inside the same container as the agent: the command is
    swapped to the bundled in-container Python, ``ARISE_SECBENCH_HELPER_SCRIPT``
    is dropped (server takes the in-container code path), and
    ``--mcp-config`` points at the container-side path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker()
    extras = dict(_container_session_extras(tmp_path))
    extras["mcp_servers"] = {
        "security_tools": {
            "command": "python",
            "args": ["-m", "plugins.security.mcp.security_tools_server"],
            "env": {
                "ARISE_SECBENCH_HELPER_SCRIPT": str(tmp_path / "secb-exec"),
                "ARISE_SECBENCH_CONTAINER_ID": "abc123def456",
                "ARISE_SECBENCH_WORK_DIR": "/src/demo",
            },
        },
    }
    workspace = WorkspaceSpec(root=tmp_path, extras=extras)

    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )

    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    assert "--mcp-config" in argv
    config_path = Path(argv[argv.index("--mcp-config") + 1])
    # The path passed to claude must be the *container* path (absolute), not
    # the host path under tmp_path.
    assert config_path.is_absolute()
    assert str(tmp_path) not in str(config_path)

    # Verify the JSON written on the host has the rewritten command + env.
    written = json.loads((tmp_path / "mcp_servers.in_container.json").read_text())
    server = written["mcpServers"]["security_tools"]
    assert server["command"] == "/opt/arise-mcp/venv/bin/python"
    assert server["args"] == ["-m", "plugins.security.mcp.security_tools_server"]
    assert "ARISE_SECBENCH_HELPER_SCRIPT" not in server["env"]
    assert server["env"]["ARISE_SECBENCH_CONTAINER_ID"] == "abc123def456"
    assert server["env"]["PYTHONPATH"] == "/opt/arise-mcp"


def test_run_task_in_container_with_global_config_skips_scratch_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: UUID
) -> None:
    """``use_global_config=True`` in containerized mode passes no
    ``CLAUDE_CONFIG_DIR`` env flag — the in-container CLI uses its own
    default config location."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")
    captured: dict[str, object] = {}

    async def _fake_exec(*args, **_kwargs):
        captured["argv"] = args
        return _fake_process(returncode=0)

    monkeypatch.setattr(
        "infrastructure.workers.claude_code_worker.asyncio.create_subprocess_exec",
        _fake_exec,
    )

    worker = ClaudeCodeWorker(use_global_config=True)
    workspace = WorkspaceSpec(root=tmp_path, extras=_container_session_extras(tmp_path))

    asyncio.run(
        worker.run_task(
            run_id=run_id,
            spec=_make_spec(),
            tool_policy=_make_policy(),
            timeouts=TimeoutBudget(per_worker_call=30, per_run_total=60),
            workspace=workspace,
        )
    )

    argv: tuple[str, ...] = captured["argv"]  # type: ignore[assignment]
    e_flags = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    forwarded_keys = {flag.split("=", 1)[0] for flag in e_flags}
    assert "CLAUDE_CONFIG_DIR" not in forwarded_keys


# ---------------------------------------------------------------------------
# Audit N-3 completion: _cost_event must sum cache + reasoning into tokens so
# cross-adapter token comparisons stay symmetric with the SDK adapter (which
# already does this) and OpenHands (which sums all five buckets).
# Pre-fix the CLI-side worker preferred `payload.total_tokens` (prompt +
# completion only) and silently dropped cache + reasoning.
# ---------------------------------------------------------------------------


def _make_cost_event(worker: ClaudeCodeWorker, payload: dict[str, object]):
    """Drive ``_cost_event`` with a minimal sequencer and assert it returns."""
    from infrastructure.adapters.worker.shared import EventSequencer

    sequencer = EventSequencer(agent_id=uuid4(), stream="claude_code")
    event = worker._cost_event(payload, sequencer, wall_time_seconds=1.0)
    assert event is not None, "expected a WorkerCostRecorded event"
    return event


def test_cost_event_sums_cache_and_reasoning_into_total() -> None:
    """All five buckets feed `tokens`; `payload.total_tokens` is ignored when
    a breakdown is reported (it under-counts cache + reasoning)."""
    worker = ClaudeCodeWorker()
    payload: dict[str, object] = {
        "type": "result",
        "total_cost_usd": 0.01,
        "total_tokens": 125,  # CLI under-reports; breakdown is the source of truth
        "usage": {
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "thinking_tokens": 7,
        },
    }

    event = _make_cost_event(worker, payload)

    assert event.prompt_tokens == 100
    assert event.completion_tokens == 25
    assert event.cache_read_tokens == 10
    assert event.cache_write_tokens == 5
    assert event.reasoning_tokens == 7
    # 100 + 25 + 10 + 5 + 7 — NOT 125
    assert event.tokens == 147


def test_cost_event_returns_none_total_when_no_breakdown_reported() -> None:
    """When no breakdown bucket is reported, `tokens` is None rather than
    silently falling back to the CLI's `total_tokens` aggregate (which is
    prompt+completion only and would re-introduce the audit N-3
    undercount asymmetry).
    """
    worker = ClaudeCodeWorker()
    payload: dict[str, object] = {
        "type": "result",
        "total_cost_usd": 0.005,
        "total_tokens": 80,
        "usage": {},
    }

    event = _make_cost_event(worker, payload)

    assert event.prompt_tokens is None
    assert event.completion_tokens is None
    assert event.reasoning_tokens is None
    assert event.tokens is None


def test_cost_event_aliases_reasoning_to_thinking() -> None:
    """``reasoning_tokens`` is the canonical key; ``thinking_tokens`` is the
    CLI's spelling. Either should populate the reasoning field."""
    worker = ClaudeCodeWorker()
    payload_with_reasoning: dict[str, object] = {
        "type": "result",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 3},
    }

    event = _make_cost_event(worker, payload_with_reasoning)

    assert event.reasoning_tokens == 3
    assert event.tokens == 18
