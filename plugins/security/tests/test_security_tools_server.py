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
    )

    # Then: it carries the python entrypoint and env contract.
    assert spec["command"] == "python"
    assert spec["args"] == ["-m", "plugins.security.mcp.security_tools_server"]
    assert spec["env"][server.ENV_CONTAINER_ID] == "abc123def456"
    assert spec["env"][server.ENV_HELPER_SCRIPT] == "/run/secb-exec"
    assert spec["env"][server.ENV_WORK_DIR] == "/src/demo"


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
