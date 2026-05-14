"""Unit tests for the SEC-bench security tools MCP server.

Subprocess invocation is mocked: docker is never spawned. Tests verify env
contract, command construction, structured error responses, lazy KLEE
install, and the engine-agnostic ``build_stdio_config`` helper.
"""

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from plugins.security.mcp import security_tools_server as server


def _ok_completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["secb-exec", "<cmd>"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the env vars the server reads at tool-call time."""
    monkeypatch.setenv(server.ENV_CONTAINER_ID, "abc123def456")
    monkeypatch.setenv(server.ENV_HELPER_SCRIPT, "/run/secb-exec")
    monkeypatch.setenv(server.ENV_WORK_DIR, "/src/demo")


def test_build_stdio_config_returns_engine_agnostic_dict() -> None:
    # Given/When: build the spec with explicit values.
    spec = server.build_stdio_config(
        container_id="abc123def456",
        helper_script="/run/secb-exec",
        work_dir="/src/demo",
        host_source_dir="/host/run/src",
        host_testcase_dir="/host/run/testcase",
        host_work_root="/host/run/work",
        workspace_root="/host/run",
    )

    # Then: it carries the python entrypoint and env contract.
    assert spec["command"] == "python"
    assert spec["args"] == ["-m", "plugins.security.mcp.security_tools_server"]
    assert spec["env"][server.ENV_CONTAINER_ID] == "abc123def456"
    assert spec["env"][server.ENV_HELPER_SCRIPT] == "/run/secb-exec"
    assert spec["env"][server.ENV_WORK_DIR] == "/src/demo"
    assert spec["env"][server.ENV_HOST_SOURCE_DIR] == "/host/run/src"
    assert spec["env"][server.ENV_HOST_TESTCASE_DIR] == "/host/run/testcase"
    assert spec["env"][server.ENV_HOST_WORK_ROOT] == "/host/run/work"
    assert spec["env"][server.ENV_WORKSPACE_ROOT] == "/host/run"
    assert spec["env"][server.ENV_CONTAINER_SOURCE_DIR] == "/src"
    assert spec["env"][server.ENV_CONTAINER_TESTCASE_DIR] == "/testcase"
    assert spec["env"][server.ENV_CONTAINER_WORK_DIR] == "/work"


def test_build_stdio_config_omits_work_dir_when_unset() -> None:
    # When: caller omits work_dir.
    spec = server.build_stdio_config(
        container_id="cid",
        helper_script="/h",
    )

    # Then: ARISE_SECBENCH_WORK_DIR is not present.
    assert server.ENV_WORK_DIR not in spec["env"]


def test_valgrind_run_returns_structured_error_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: env vars are NOT set.
    monkeypatch.delenv(server.ENV_CONTAINER_ID, raising=False)
    monkeypatch.delenv(server.ENV_HELPER_SCRIPT, raising=False)

    # When: tool is invoked.
    result = server.valgrind_run(target_path="./bin", args=["arg1"])

    # Then: structured error is returned, no exception raised.
    assert result["ok"] is False
    assert result["error"] == "missing_env"
    assert server.ENV_CONTAINER_ID in result["message"]


def test_valgrind_run_uses_bash_lc_in_in_container_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ARISE_SECBENCH_HELPER_SCRIPT is unset, the server is running
    inside the target container (Cell A path). It must execute commands
    directly via ``bash -lc`` instead of shelling out to ``secb-exec``."""
    # Given: only in-container env vars set (no helper script).
    monkeypatch.delenv(server.ENV_HELPER_SCRIPT, raising=False)
    monkeypatch.setenv(server.ENV_CONTAINER_ID, "abc123def456")
    monkeypatch.setenv(server.ENV_WORK_DIR, "/src/demo")
    captured: dict[str, Any] = {}

    def _fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _ok_completed(stdout="leaks: 0\n", returncode=0)

    with patch.object(server.subprocess, "run", _fake_run):
        result = server.valgrind_run(target_path="./bin", args=["--flag"])

    # Then: argv invokes bash -lc, NOT secb-exec.
    assert captured["argv"][0] == "bash"
    assert captured["argv"][1] == "-lc"
    cmd = captured["argv"][2]
    assert "cd /src/demo &&" in cmd
    assert "valgrind" in cmd
    assert "--flag" in cmd
    assert result["ok"] is True


@pytest.mark.usefixtures("env")
def test_valgrind_run_invokes_helper_with_default_options() -> None:
    # Given: subprocess.run will be captured.
    captured: dict[str, Any] = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs.get("timeout")
        return _ok_completed(stdout="leaks: 0\n", returncode=0)

    with patch.object(server.subprocess, "run", _fake_run):
        result = server.valgrind_run(target_path="./bin", args=["--flag"])

    # Then: helper script invoked with the expected command shape.
    assert captured["argv"][0] == "/run/secb-exec"
    cmd = captured["argv"][1]
    assert "cd /src/demo &&" in cmd
    assert "valgrind" in cmd
    assert "--tool=memcheck" in cmd
    assert "--leak-check=full" in cmd
    assert "--error-exitcode=1" in cmd
    assert "./bin" in cmd
    assert "--flag" in cmd
    # And: result payload is structured.
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "leaks: 0\n"


@pytest.mark.usefixtures("env")
def test_valgrind_run_honors_custom_options() -> None:
    captured: dict[str, Any] = {}

    def _fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _ok_completed()

    with patch.object(server.subprocess, "run", _fake_run):
        server.valgrind_run(
            target_path="./bin",
            args=[],
            options=["--tool=helgrind"],
        )

    cmd = captured["argv"][1]
    assert "--tool=helgrind" in cmd
    # Default options should NOT be present when override given.
    assert "--leak-check=full" not in cmd


@pytest.mark.usefixtures("env")
def test_valgrind_run_returns_failure_when_helper_missing() -> None:
    def _raise(*_args, **_kwargs):
        raise FileNotFoundError("no such file: /run/secb-exec")

    with patch.object(server.subprocess, "run", _raise):
        result = server.valgrind_run(target_path="./bin")

    assert result["ok"] is False
    assert result["error"] == "helper_missing"


@pytest.mark.usefixtures("env")
def test_valgrind_run_returns_failure_on_timeout() -> None:
    def _raise(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=5)

    with patch.object(server.subprocess, "run", _raise):
        result = server.valgrind_run(target_path="./bin")

    assert result["ok"] is False
    assert result["error"] == "timeout"


@pytest.mark.usefixtures("env")
def test_klee_run_installs_lazily_then_runs() -> None:
    calls: list[str] = []

    def _fake_run(argv, **_kwargs):
        cmd = argv[1]
        calls.append(cmd)
        # Probe returns non-zero so the install path runs; install + klee succeed.
        returncode = 1 if cmd == server._KLEE_PROBE_CMD else 0
        return _ok_completed(returncode=returncode)

    with patch.object(server.subprocess, "run", _fake_run):
        result = server.klee_run(bitcode_path="target.bc", max_time_seconds=30)

    # Then: probe runs first, install second, klee third.
    assert len(calls) == 3
    assert calls[0] == server._KLEE_PROBE_CMD
    assert calls[1] == server._KLEE_INSTALL_CMD
    assert "klee" in calls[2]
    assert "--max-time=30" in calls[2]
    assert "--output-dir=klee-out" in calls[2]
    assert "target.bc" in calls[2]
    assert result["ok"] is True


@pytest.mark.usefixtures("env")
def test_klee_run_skips_install_when_already_present() -> None:
    calls: list[str] = []

    def _fake_run(argv, **_kwargs):
        calls.append(argv[1])
        # Probe succeeds; should skip install and go straight to klee.
        return _ok_completed()

    with patch.object(server.subprocess, "run", _fake_run):
        result = server.klee_run(bitcode_path="target.bc")

    assert len(calls) == 2
    assert calls[0] == server._KLEE_PROBE_CMD
    assert "klee" in calls[1]
    assert server._KLEE_INSTALL_CMD not in calls
    assert result["ok"] is True


@pytest.mark.usefixtures("env")
def test_klee_run_returns_install_error_when_apt_fails() -> None:
    def _fake_run(_argv, **_kwargs):
        # Probe fails (klee not installed), then install also fails.
        return _ok_completed(stderr="E: Unable to locate package klee", returncode=1)

    with patch.object(server.subprocess, "run", _fake_run):
        result = server.klee_run(bitcode_path="target.bc")

    assert result["ok"] is False
    assert result["error"] == "klee_install_failed"
    assert "valgrind" in result["message"].lower()
    assert result["install_exit_code"] == 1


# -- shell_in_container (E.10) -------------------------------------------------


def test_shell_in_container_invokes_exec_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Host-mode call routes the command through ``secb-exec`` (docker exec).

    The helper script gets the command verbatim — with the configured
    working directory prefixed via ``cd <work_dir> && ...``. We use a
    real shell script as the "helper" so the captured output proves end
    to end that argv[1] is what the agent intends to run inside the
    container.
    """
    # Given: a fake helper script that echoes its single positional arg.
    helper = tmp_path / "fake-helper.sh"
    helper.write_text('#!/usr/bin/env bash\necho "HELPER GOT: $1"\n')
    helper.chmod(0o755)

    monkeypatch.setenv(server.ENV_CONTAINER_ID, "abc123def456")
    monkeypatch.setenv(server.ENV_HELPER_SCRIPT, str(helper))
    monkeypatch.setenv(server.ENV_WORK_DIR, "/src/demo")

    # When: the agent invokes ``shell_in_container`` with a build command.
    result = server.shell_in_container(command="make all")

    # Then: the helper was invoked with the agent's command (prefixed by
    # the configured cwd).
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "HELPER GOT:" in result["stdout"]
    assert "cd /src/demo && make all" in result["stdout"]


def test_shell_in_container_translates_host_mirror_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    # Given: a helper script captures the command routed into the container.
    helper = tmp_path / "fake-helper.sh"
    helper.write_text('#!/usr/bin/env bash\necho "HELPER GOT: $1"\n')
    helper.chmod(0o755)
    monkeypatch.setenv(server.ENV_CONTAINER_ID, "abc123def456")
    monkeypatch.setenv(server.ENV_HELPER_SCRIPT, str(helper))
    monkeypatch.setenv(server.ENV_WORK_DIR, "/src/openjpeg")
    monkeypatch.setenv(server.ENV_HOST_SOURCE_DIR, "/host/run/src")
    monkeypatch.setenv(server.ENV_HOST_TESTCASE_DIR, "/host/run/testcase")
    monkeypatch.setenv(server.ENV_HOST_WORK_ROOT, "/host/run/work")
    monkeypatch.setenv(server.ENV_WORKSPACE_ROOT, "/host/run")
    monkeypatch.setenv(server.ENV_CONTAINER_SOURCE_DIR, "/src")
    monkeypatch.setenv(server.ENV_CONTAINER_TESTCASE_DIR, "/testcase")
    monkeypatch.setenv(server.ENV_CONTAINER_WORK_DIR, "/work")
    monkeypatch.setenv(server.ENV_CONTAINER_WORKSPACE_ROOT, "/arise-run")

    # When: the model incorrectly passes host mirror paths to the container shell.
    result = server.shell_in_container(
        command=(
            "cd /host/run/src/openjpeg && "
            "cp /host/run/testcase/poc /host/run/work/poc.copy"
        )
    )

    # Then: the MCP server rewrites them to container paths before execution.
    assert result["ok"] is True
    assert "cd /src/openjpeg && cd /src/openjpeg" in result["stdout"]
    assert "cp /testcase/poc /work/poc.copy" in result["stdout"]
    assert "/host/run" not in result["stdout"]


@pytest.mark.usefixtures("env")
def test_valgrind_run_translates_host_target_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: path translation env is present.
    monkeypatch.setenv(server.ENV_HOST_SOURCE_DIR, "/host/run/src")
    captured: dict[str, Any] = {}

    def _fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _ok_completed()

    # When: target_path is a host mirror path.
    with patch.object(server.subprocess, "run", _fake_run):
        server.valgrind_run(target_path="/host/run/src/demo/bin/app")

    # Then: Valgrind is executed against the container path.
    assert "/src/demo/bin/app" in captured["argv"][1]
    assert "/host/run/src" not in captured["argv"][1]


def test_shell_in_container_works_in_container_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-container mode (no helper) executes via ``bash -lc``."""
    # Given: only the container id is set (in-container deployment).
    monkeypatch.delenv(server.ENV_HELPER_SCRIPT, raising=False)
    monkeypatch.delenv(server.ENV_WORK_DIR, raising=False)
    monkeypatch.setenv(server.ENV_CONTAINER_ID, "abc123def456")

    captured: dict[str, Any] = {}

    def _fake_run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return _ok_completed(stdout="hi\n", returncode=0)

    # When: the agent calls ``shell_in_container``.
    with patch.object(server.subprocess, "run", _fake_run):
        result = server.shell_in_container(command="echo hi")

    # Then: the in-container fast path was taken — ``bash -lc <command>``,
    # not ``secb-exec``.
    assert captured["argv"][0] == "bash"
    assert captured["argv"][1] == "-lc"
    assert captured["argv"][2] == "echo hi"
    assert result["ok"] is True


def test_shell_in_container_returns_structured_error_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env returns the same ``missing_env`` error as the other tools."""
    monkeypatch.delenv(server.ENV_CONTAINER_ID, raising=False)
    monkeypatch.delenv(server.ENV_HELPER_SCRIPT, raising=False)

    result = server.shell_in_container(command="anything")

    assert result["ok"] is False
    assert result["error"] == "missing_env"
