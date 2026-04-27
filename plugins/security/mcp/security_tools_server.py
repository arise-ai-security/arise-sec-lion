"""Stdio MCP server exposing SEC-bench security tools to worker engines.

Two tools are exposed:

- ``valgrind_run`` — runs Valgrind memcheck against a compiled binary.
- ``klee_run`` — runs KLEE symbolic execution against an LLVM bitcode file
  after lazily installing KLEE inside the active container on first call.

Both tools shell into the active per-run secb-tools container via the
``secb-exec`` helper script. The container id and helper-script path are
read from the environment (``ARISE_SECBENCH_CONTAINER_ID`` and
``ARISE_SECBENCH_HELPER_SCRIPT``). Errors return structured failure dicts
rather than raising — the agent then sees a normal MCP error result.

Run as a stdio subprocess: ``python -m plugins.security.mcp.security_tools_server``.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP


logger = logging.getLogger(__name__)

ENV_CONTAINER_ID = "ARISE_SECBENCH_CONTAINER_ID"
ENV_HELPER_SCRIPT = "ARISE_SECBENCH_HELPER_SCRIPT"
ENV_WORK_DIR = "ARISE_SECBENCH_WORK_DIR"

_DEFAULT_VALGRIND_OPTIONS = (
    "--tool=memcheck",
    "--leak-check=full",
    "--error-exitcode=1",
)
_KLEE_PROBE_CMD = "command -v klee"
_KLEE_INSTALL_CMD = "apt-get update && apt-get install -y klee"

_DEFAULT_TIMEOUT_SECONDS = 600

mcp = FastMCP("arise-secbench-tools")


def build_stdio_config(
    *,
    container_id: str,
    helper_script: str,
    work_dir: str | None = None,
    python_executable: str = "python",
) -> dict[str, Any]:
    """Build the engine-agnostic stdio server spec for adapter wiring.

    Returns the inner per-server dict (``command``, ``args``, ``env``) — each
    adapter wraps it in the engine-specific shape (CLI ``mcpServers``, SDK
    ``mcp_servers`` kwarg, OpenHands ``MCPConfig``).
    """
    env: dict[str, str] = {
        ENV_CONTAINER_ID: container_id,
        ENV_HELPER_SCRIPT: helper_script,
    }
    if work_dir is not None:
        env[ENV_WORK_DIR] = work_dir
    return {
        "command": python_executable,
        "args": ["-m", "plugins.security.mcp.security_tools_server"],
        "env": env,
    }


def _read_env() -> tuple[str | None, str | None, str | None] | dict[str, Any]:
    """Read env vars and pick a transport mode.

    Two transport modes are supported:

    - **Host mode** (``ARISE_SECBENCH_HELPER_SCRIPT`` set): the MCP server runs
      on the host. Commands are routed into the per-run secb-tools container
      via the ``secb-exec`` helper, which does ``docker exec``.
    - **In-container mode** (helper script unset): the MCP server runs *inside*
      the secb-tools container alongside the agent (Cell A). Commands execute
      directly via ``bash -lc`` — no ``docker exec`` hop, since we're already
      in the target container.

    Returns ``(container_id, helper_script, work_dir)`` where ``helper_script``
    is ``None`` in in-container mode. ``container_id`` is informational only
    in in-container mode.
    """
    container_id = os.environ.get(ENV_CONTAINER_ID, "").strip() or None
    helper_script = os.environ.get(ENV_HELPER_SCRIPT, "").strip() or None
    work_dir = os.environ.get(ENV_WORK_DIR) or None
    if helper_script is None and container_id is None:
        return {
            "ok": False,
            "error": "missing_env",
            "message": (
                "Neither host-mode env (ARISE_SECBENCH_HELPER_SCRIPT) nor "
                "in-container env (ARISE_SECBENCH_CONTAINER_ID) is set. "
                "At least one must be injected by the worker adapter."
            ),
        }
    return container_id, helper_script, work_dir


def _exec_in_container(
    helper_script: str | None,
    command: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``command`` either via the host-side helper or directly inside the container.

    When ``helper_script`` is set we shell out to ``helper_script "<command>"``
    (host mode — the helper does ``docker exec``). When it is ``None`` we run
    the command via ``bash -lc <command>`` in the local shell (in-container
    mode — the MCP server is already inside the target container).
    """
    argv = ["bash", "-lc", command] if helper_script is None else [helper_script, command]
    try:
        completed = subprocess.run(  # noqa: S603 - argv built from trusted env / sanitized command
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": "helper_missing" if helper_script else "shell_missing",
            "message": f"Failed to launch {argv[0]!r}: {exc}",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": "timeout",
            "message": (f"Command exceeded {timeout_seconds}s timeout: {exc.cmd!r}"),
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": "helper_exec_failed",
            "message": f"Failed to invoke command: {exc}",
        }

    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
    }


def _build_valgrind_command(
    target_path: str,
    args: list[str],
    options: list[str] | None,
    work_dir: str | None,
) -> str:
    opts = list(options) if options else list(_DEFAULT_VALGRIND_OPTIONS)
    parts = ["valgrind", *opts, target_path, *args]
    quoted = " ".join(shlex.quote(part) for part in parts)
    if work_dir:
        return f"cd {shlex.quote(work_dir)} && {quoted}"
    return quoted


def _build_klee_command(
    bitcode_path: str,
    max_time_seconds: int,
    output_dir: str,
    work_dir: str | None,
) -> str:
    parts = [
        "klee",
        f"--max-time={max_time_seconds}",
        f"--output-dir={output_dir}",
        bitcode_path,
    ]
    quoted = " ".join(shlex.quote(part) for part in parts)
    if work_dir:
        return f"cd {shlex.quote(work_dir)} && {quoted}"
    return quoted


@mcp.tool(
    name="valgrind_run",
    description=(
        "Run Valgrind memcheck against a compiled binary inside the SEC-bench "
        "container. Returns exit code, stdout, and stderr. Default options are "
        "--tool=memcheck --leak-check=full --error-exitcode=1; pass `options` "
        "to override."
    ),
)
def valgrind_run(
    target_path: str,
    args: list[str] | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """Run valgrind on ``target_path`` inside the active container."""
    env = _read_env()
    if isinstance(env, dict):
        return env
    _container_id, helper_script, work_dir = env
    command = _build_valgrind_command(
        target_path=target_path,
        args=args or [],
        options=options,
        work_dir=work_dir,
    )
    return _exec_in_container(helper_script, command)


@mcp.tool(
    name="klee_run",
    description=(
        "Run KLEE symbolic execution against an LLVM bitcode file inside the "
        "SEC-bench container. KLEE is installed lazily on first call via "
        "`apt-get install -y klee` (idempotent). Returns exit code, stdout, "
        "stderr. If installation fails, returns a structured error rather than "
        "raising — fall back to valgrind in that case."
    ),
)
def klee_run(
    bitcode_path: str,
    max_time_seconds: int = 120,
    output_dir: str = "klee-out",
) -> dict[str, Any]:
    """Run KLEE on ``bitcode_path`` inside the active container."""
    env = _read_env()
    if isinstance(env, dict):
        return env
    _container_id, helper_script, work_dir = env

    probe = _exec_in_container(helper_script, _KLEE_PROBE_CMD)
    if not (probe.get("ok") and int(probe.get("exit_code", 1)) == 0):
        install_result = _exec_in_container(helper_script, _KLEE_INSTALL_CMD)
        if not install_result.get("ok") or int(install_result.get("exit_code", 1)) != 0:
            return {
                "ok": False,
                "error": "klee_install_failed",
                "message": (
                    "Failed to install KLEE in the container. "
                    "Falling back to valgrind is recommended."
                ),
                "install_exit_code": install_result.get("exit_code"),
                "install_stdout": install_result.get("stdout"),
                "install_stderr": install_result.get("stderr"),
            }

    command = _build_klee_command(
        bitcode_path=bitcode_path,
        max_time_seconds=max_time_seconds,
        output_dir=output_dir,
        work_dir=work_dir,
    )
    return _exec_in_container(
        helper_script,
        command,
        timeout_seconds=max(_DEFAULT_TIMEOUT_SECONDS, max_time_seconds + 60),
    )


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
