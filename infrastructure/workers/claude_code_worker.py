"""Worker that invokes the Claude Code CLI (``claude -p ...``) for a single task.

Conforms to ``core.ports.worker_port.WorkerPort``. Replaces the legacy
baseline runner for execution under the unified pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.application.run_invariants import (
    WorkerResult,
    build_env_policy,
)
from core.domain.events.events import DomainEvent  # noqa: TC001
from core.domain.exceptions import ToolNotAvailableError
from infrastructure.adapters.worker.shared import (
    ContainerSessionContext,
    EventSequencer,
    UsageBreakdown,
    emit_cost,
    format_tool_event,
    to_cli_config_payload,
)


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkspaceSpec,
    )


logger = logging.getLogger(__name__)


_DEFAULT_OUTPUT_FORMAT = "stream-json"
_DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 600
_CONTAINER_AGENT_UID = "1000"
_CONTAINER_AGENT_GID = "1000"
_CONTAINER_AGENT_USER = "arise-agent"
_CONTAINER_AGENT_HOME = "/tmp/arise-claude-home"  # noqa: S108
_CONTAINER_ROOT_HELPER = "/usr/local/bin/arise-root"
_CONTAINER_ROOT_BASH = "/usr/local/bin/arise-root-bash"
_CONTAINER_SUDO_SHIM = "/usr/local/bin/sudo"
_CONTAINER_APT_GET_SHIM = "/usr/local/bin/apt-get"
_CONTAINER_USER_PREP_TIMEOUT_SECONDS = 10
_PROCESS_KILL_GRACE_SECONDS = 5

# In-container exec path forwards a narrow allowlist only — the container has
# its own coherent base env (PATH, HOME, etc.). Forwarding host PATH would
# shadow the container's PATH and break ``claude`` resolution.
_CONTAINER_ENV_ALLOWLIST: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "TZ"})


class ClaudeCodeWorker:
    """Adapter satisfying ``WorkerPort`` by invoking the ``claude`` CLI.

    By default, a scratch ``CLAUDE_CONFIG_DIR`` is materialized per call so the
    CLI reads only the policy our ``ToolPolicy`` declares. Callers can opt into
    the operator/global Claude config via ``use_global_config`` while still
    receiving the same ``ToolPolicy`` CLI allow/deny flags as every other
    worker engine.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        output_format: str = _DEFAULT_OUTPUT_FORMAT,
        include_partial_messages: bool = True,
        max_turns: int | None = None,
        use_global_config: bool = False,
        env_allowlist: frozenset[str] | None = None,
        inactivity_timeout_seconds: int | None = _DEFAULT_INACTIVITY_TIMEOUT_SECONDS,
    ) -> None:
        self._model = model
        self._output_format = output_format
        self._include_partial_messages = include_partial_messages
        self._max_turns = max_turns
        self._use_global_config = use_global_config
        self._env_allowlist = env_allowlist or build_env_policy()
        self._inactivity_timeout_seconds = inactivity_timeout_seconds

    async def run_task(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
    ) -> WorkerResult:
        run_dir = Path(workspace.root)
        run_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = run_dir / "stdout_stderr.log"

        container_session = ContainerSessionContext.from_task_context(
            {"container_session": workspace.extras.get("container_session")}
        )
        if container_session is not None:
            return await self._run_in_container(
                run_id=run_id,
                spec=spec,
                tool_policy=tool_policy,
                timeouts=timeouts,
                workspace=workspace,
                run_dir=run_dir,
                transcript_path=transcript_path,
                container_session=container_session,
            )
        return await self._run_on_host(
            run_id=run_id,
            spec=spec,
            tool_policy=tool_policy,
            timeouts=timeouts,
            workspace=workspace,
            run_dir=run_dir,
            transcript_path=transcript_path,
        )

    async def _run_on_host(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
        run_dir: Path,
        transcript_path: Path,
    ) -> WorkerResult:
        claude_bin = shutil.which("claude")
        if claude_bin is None:
            raise ToolNotAvailableError(
                tool_name="claude",
                available_tools=[],
            )

        mcp_config_path = self._maybe_write_mcp_config(workspace, run_dir)

        if self._use_global_config:
            argv = self._build_argv(
                claude_bin=claude_bin,
                rendered_prompt=self._render_prompt(spec.rendered_prompt, workspace),
                tool_policy=tool_policy,
                mcp_config_path=mcp_config_path,
            )
            env = self._build_env(scratch_dir=None)
            return await self._exec(
                argv=argv,
                env=env,
                cwd=run_dir,
                transcript_path=transcript_path,
                timeout_seconds=timeouts.per_worker_call,
                run_id=run_id,
            )

        with tempfile.TemporaryDirectory(prefix="claude-cfg-") as scratch:
            scratch_dir = Path(scratch)
            self._write_settings(scratch_dir, tool_policy)
            argv = self._build_argv(
                claude_bin=claude_bin,
                rendered_prompt=self._render_prompt(spec.rendered_prompt, workspace),
                tool_policy=tool_policy,
                mcp_config_path=mcp_config_path,
            )
            env = self._build_env(scratch_dir=scratch_dir)
            return await self._exec(
                argv=argv,
                env=env,
                cwd=run_dir,
                transcript_path=transcript_path,
                timeout_seconds=timeouts.per_worker_call,
                run_id=run_id,
            )

    async def _run_in_container(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
        run_dir: Path,
        transcript_path: Path,
        container_session: ContainerSessionContext,
    ) -> WorkerResult:
        """Container path: ``claude`` runs inside the secb-tools container.

        ``mcp_servers`` from ``workspace.extras`` is rewritten for in-container
        execution: the JSON config and stdio command are remapped so the MCP
        server runs inside the same container as the agent (no host-side
        ``secb-exec`` hop). The Dockerfile bakes Python + the ``mcp`` package
        and the server module under ``/opt/arise-mcp/``.
        """
        # Inside the container, ``claude`` is on PATH (installed by the
        # secb-tools Dockerfile); no host shutil.which probe.
        in_container_claude = "claude"
        await self._prepare_in_container_user(container_session)
        mcp_config_container_path = self._maybe_write_in_container_mcp_config(
            workspace=workspace,
            run_dir=run_dir,
            container_session=container_session,
        )

        if self._use_global_config:
            claude_argv = self._build_argv(
                claude_bin=in_container_claude,
                rendered_prompt=spec.rendered_prompt,
                tool_policy=tool_policy,
                mcp_config_path=mcp_config_container_path,
            )
            container_env = self._build_container_env(scratch_container_dir=None)
            argv = self._wrap_with_docker_exec(
                claude_argv=claude_argv,
                container_session=container_session,
                env=container_env,
            )
            return await self._exec(
                argv=argv,
                env=self._build_env(scratch_dir=None),
                cwd=run_dir,
                transcript_path=transcript_path,
                timeout_seconds=timeouts.per_worker_call,
                run_id=run_id,
            )

        # Scratch CLAUDE_CONFIG_DIR must live under host_root so it is reachable
        # from inside the container via the workspace bind mount.
        with tempfile.TemporaryDirectory(dir=str(run_dir), prefix="claude-cfg-") as scratch:
            scratch_dir = Path(scratch)
            self._write_settings(scratch_dir, tool_policy)
            scratch_container_dir = container_session.host_to_container_path(scratch_dir)
            claude_argv = self._build_argv(
                claude_bin=in_container_claude,
                rendered_prompt=spec.rendered_prompt,
                tool_policy=tool_policy,
                mcp_config_path=mcp_config_container_path,
            )
            container_env = self._build_container_env(scratch_container_dir=scratch_container_dir)
            argv = self._wrap_with_docker_exec(
                claude_argv=claude_argv,
                container_session=container_session,
                env=container_env,
            )
            return await self._exec(
                argv=argv,
                env=self._build_env(scratch_dir=None),
                cwd=run_dir,
                transcript_path=transcript_path,
                timeout_seconds=timeouts.per_worker_call,
                run_id=run_id,
            )

    def _maybe_write_in_container_mcp_config(
        self,
        *,
        workspace: WorkspaceSpec,
        run_dir: Path,
        container_session: ContainerSessionContext,
    ) -> Path | None:
        """Write an ``mcp_servers.json`` rewritten for in-container execution.

        The host-side stdio config (built by the security plugin) tells the MCP
        server to call ``secb-exec`` to docker-exec into the container. When
        ``claude`` itself runs *inside* that container, the helper script is
        unreachable; instead we rewrite each server's ``command``/``args`` to
        invoke the in-container Python + the bundled MCP module, and drop
        ``ARISE_SECBENCH_HELPER_SCRIPT`` so the server takes the
        in-container code path (``bash -lc`` direct).

        The JSON file lands in the bind-mounted run directory so it is visible
        from both host and container; the returned path is the *container*
        path so ``--mcp-config`` resolves correctly inside the container.
        """
        servers = workspace.extras.get("mcp_servers")
        if not isinstance(servers, dict) or not servers:
            return None
        rewritten: dict[str, dict[str, Any]] = {}
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            entry = dict(spec)
            entry["command"] = "/opt/arise-mcp/venv/bin/python"
            entry["args"] = ["-m", "plugins.security.mcp.security_tools_server"]
            new_env = {
                k: v
                for k, v in (entry.get("env") or {}).items()
                if k != "ARISE_SECBENCH_HELPER_SCRIPT"
            }
            new_env.setdefault("PYTHONPATH", "/opt/arise-mcp")
            entry["env"] = new_env
            rewritten[name] = entry
        if not rewritten:
            return None
        host_target = run_dir / "mcp_servers.in_container.json"
        host_target.write_text(
            json.dumps(to_cli_config_payload(rewritten), indent=2),
            encoding="utf-8",
        )
        return Path(container_session.host_to_container_path(host_target))

    def _build_argv(
        self,
        *,
        claude_bin: str,
        rendered_prompt: str,
        tool_policy: ToolPolicy,
        mcp_config_path: Path | None = None,
    ) -> list[str]:
        argv = [
            claude_bin,
            "-p",
            "--output-format",
            self._output_format,
        ]
        # ``--print --output-format=stream-json`` requires ``--verbose`` per
        # the Claude Code CLI; otherwise it errors with "stream-json requires
        # --verbose". Always pair them when stream-json is selected.
        if self._output_format == "stream-json":
            argv.append("--verbose")
        # The Claude CLI's default permission mode requires interactive
        # approval for every tool call. In a batch / sandboxed experiment run
        # there is no operator to click "approve", so the agent burns turns on
        # denials and hits max-turns without making progress. Tool policy is
        # still enforced via ``--allowedTools`` / ``--disallowedTools`` and
        # the scratch ``settings.json`` we write — bypass only short-circuits
        # the per-call interactive gate. ``bypassPermissions`` is preferred over
        # ``--dangerously-skip-permissions`` because the latter refuses to run
        # as root, and the secb-tools container runs as root by default.
        argv.extend(["--permission-mode", "bypassPermissions"])
        if self._model:
            argv.extend(["--model", self._model])
        if self._max_turns is not None:
            argv.extend(["--max-turns", str(self._max_turns)])
        if self._include_partial_messages:
            argv.append("--include-partial-messages")
        if mcp_config_path is not None:
            argv.extend(["--mcp-config", str(mcp_config_path)])
        if tool_policy.allowed != ("*",):
            for tool in tool_policy.allowed:
                argv.extend(["--allowedTools", tool])
        for tool in tool_policy.disallowed:
            argv.extend(["--disallowedTools", tool])
        # End-of-flags separator: ``--mcp-config`` is variadic in the Claude Code
        # CLI, so the rendered prompt would otherwise be interpreted as a second
        # config path and the launch would fail with ENAMETOOLONG. ``--`` marks
        # everything after it as a positional argument.
        argv.append("--")
        argv.append(rendered_prompt)
        return argv

    def _maybe_write_mcp_config(self, workspace: WorkspaceSpec, run_dir: Path) -> Path | None:
        """Materialize ``workspace.extras['mcp_servers']`` to a JSON file.

        Returns the absolute path to the written ``mcp_servers.json`` when the
        extras carries a non-empty mapping, else ``None`` (CLI runs without
        ``--mcp-config``).
        """
        servers = workspace.extras.get("mcp_servers")
        if not isinstance(servers, dict) or not servers:
            return None
        payload = to_cli_config_payload(servers)
        target = run_dir / "mcp_servers.json"
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target.resolve()

    def _render_prompt(self, prompt: str, workspace: WorkspaceSpec) -> str:
        """Attach container instructions when flat mode prepared a container."""
        container_session = ContainerSessionContext.from_task_context(
            {"container_session": workspace.extras.get("container_session")}
        )
        if container_session is None:
            return prompt
        # Direct CLI mode cannot rewrite Bash tool calls with SDK hooks, so the
        # prompt requires explicit helper usage for build/test commands.
        return container_session.apply_task_prefix(prompt, auto_shell=False)

    def _build_env(self, *, scratch_dir: Path | None) -> dict[str, str]:
        """Allowlist-only env, optionally plus a scratch ``CLAUDE_CONFIG_DIR``."""
        env: dict[str, str] = {}
        for key in self._env_allowlist:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        if scratch_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(scratch_dir)
        return env

    def _build_container_env(self, *, scratch_container_dir: str | None) -> dict[str, str]:
        """In-container env: narrow allowlist + remapped ``CLAUDE_CONFIG_DIR``.

        We deliberately do NOT forward ``PATH``/``HOME``/``USER``/``LANG`` etc.
        from the host — the secb-tools container has its own coherent base
        environment and forwarding host values would override container PATH
        and break ``claude`` resolution.
        """
        env: dict[str, str] = {}
        for key in _CONTAINER_ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env["HOME"] = _CONTAINER_AGENT_HOME
        env["USER"] = _CONTAINER_AGENT_USER
        env["LOGNAME"] = _CONTAINER_AGENT_USER
        env["SHELL"] = "/bin/bash"
        if scratch_container_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = scratch_container_dir
        return env

    def _wrap_with_docker_exec(
        self,
        *,
        claude_argv: list[str],
        container_session: ContainerSessionContext,
        env: dict[str, str],
    ) -> list[str]:
        """Wrap ``claude_argv`` so it runs inside ``container_session``.

        The Claude CLI refuses to honor ``--permission-mode bypassPermissions``
        (or ``--dangerously-skip-permissions``) when running as uid 0. The
        secb-tools image inherits root from the SEC-bench base, so we drop
        privileges only for this exec by passing ``--user 1000:1000`` after
        preparing a matching passwd/group/home entry in the running container.
        The bind-mounted source and testcase trees are already
        world-readable/writable (``DockerSecBenchRuntime._copy_source_tree``
        chmods them 0o777/0o666).
        """
        wrapped: list[str] = ["docker", "exec", "-i"]
        wrapped.extend(["--user", f"{_CONTAINER_AGENT_UID}:{_CONTAINER_AGENT_GID}"])
        wrapped.extend(["-w", container_session.container_working_directory])
        for key, value in env.items():
            wrapped.extend(["-e", f"{key}={value}"])
        wrapped.append(container_session.container_id)
        wrapped.extend(claude_argv)
        return wrapped

    async def _prepare_in_container_user(
        self,
        container_session: ContainerSessionContext,
    ) -> None:
        """Create the non-root Claude user identity inside a running container."""
        script = (
            "set -eu\n"
            f"if ! grep -q '^[^:]*:[^:]*:{_CONTAINER_AGENT_GID}:' /etc/group; then\n"
            f"  printf '%s\\n' '{_CONTAINER_AGENT_USER}:x:{_CONTAINER_AGENT_GID}:' "
            ">> /etc/group\n"
            "fi\n"
            f"if ! grep -q '^[^:]*:[^:]*:{_CONTAINER_AGENT_UID}:' /etc/passwd; then\n"
            "  printf '%s\\n' "
            f"'{_CONTAINER_AGENT_USER}:x:{_CONTAINER_AGENT_UID}:{_CONTAINER_AGENT_GID}:"
            f"Arise Claude User:{_CONTAINER_AGENT_HOME}:/bin/bash' >> /etc/passwd\n"
            "fi\n"
            f"mkdir -p {_CONTAINER_AGENT_HOME} /tmp/claude-{_CONTAINER_AGENT_UID}\n"
            f"chown -R {_CONTAINER_AGENT_UID}:{_CONTAINER_AGENT_GID} "
            f"{_CONTAINER_AGENT_HOME} /tmp/claude-{_CONTAINER_AGENT_UID}\n"
            f"chmod 700 {_CONTAINER_AGENT_HOME}\n"
            "install -d -m 0755 /usr/local/bin\n"
            f"if [ ! -x {_CONTAINER_ROOT_HELPER} ]; then\n"
            "  cat > /tmp/arise-root.c <<'ARISE_ROOT_C'\n"
            "#include <errno.h>\n"
            "#include <stdio.h>\n"
            "#include <unistd.h>\n"
            "\n"
            "int main(int argc, char *argv[]) {\n"
            "    if (argc < 2) {\n"
            "        fputs(\"usage: arise-root <command> [args...]\\n\", stderr);\n"
            "        return 2;\n"
            "    }\n"
            "    if (setgid(0) != 0) {\n"
            "        perror(\"setgid\");\n"
            "        return 126;\n"
            "    }\n"
            "    if (setuid(0) != 0) {\n"
            "        perror(\"setuid\");\n"
            "        return 126;\n"
            "    }\n"
            "    execvp(argv[1], &argv[1]);\n"
            "    perror(\"execvp\");\n"
            "    return errno == ENOENT ? 127 : 126;\n"
            "}\n"
            "ARISE_ROOT_C\n"
            "  compiler=$(command -v cc || command -v gcc || command -v clang || true)\n"
            f"  if [ -n \"$compiler\" ] && \"$compiler\" -O2 -Wall -Wextra "
            f"-o {_CONTAINER_ROOT_HELPER} /tmp/arise-root.c; then\n"
            f"    chown root:root {_CONTAINER_ROOT_HELPER}\n"
            f"    chmod 4755 {_CONTAINER_ROOT_HELPER}\n"
            "  elif [ -x /bin/bash ]; then\n"
            f"    cp /bin/bash {_CONTAINER_ROOT_BASH}\n"
            f"    chown root:root {_CONTAINER_ROOT_BASH}\n"
            f"    chmod 4755 {_CONTAINER_ROOT_BASH}\n"
            f"    cat > {_CONTAINER_ROOT_HELPER} <<'ARISE_ROOT_SH'\n"
            "#!/bin/sh\n"
            f"exec {_CONTAINER_ROOT_BASH} -p -c 'exec \"$@\"' arise-root \"$@\"\n"
            "ARISE_ROOT_SH\n"
            f"    chmod 0755 {_CONTAINER_ROOT_HELPER}\n"
            "  else\n"
            "    echo 'no compiler or bash available to create root helper' >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  rm -f /tmp/arise-root.c\n"
            "fi\n"
            f"cat > {_CONTAINER_SUDO_SHIM} <<'ARISE_SUDO_SH'\n"
            "#!/bin/sh\n"
            "if [ \"$#\" -eq 0 ]; then\n"
            "  echo 'usage: sudo <command> [args...]' >&2\n"
            "  exit 2\n"
            "fi\n"
            f"exec {_CONTAINER_ROOT_HELPER} \"$@\"\n"
            "ARISE_SUDO_SH\n"
            f"chmod 0755 {_CONTAINER_SUDO_SHIM}\n"
            f"cat > {_CONTAINER_APT_GET_SHIM} <<'ARISE_APT_GET_SH'\n"
            "#!/bin/sh\n"
            f"exec {_CONTAINER_ROOT_HELPER} /usr/bin/apt-get \"$@\"\n"
            "ARISE_APT_GET_SH\n"
            f"chmod 0755 {_CONTAINER_APT_GET_SHIM}\n"
        )
        argv = [
            "docker",
            "exec",
            "-i",
            container_session.container_id,
            "bash",
            "-lc",
            script,
        ]
        logger.debug(
            "ClaudeCodeWorker preparing non-root user %s in container %s",
            _CONTAINER_AGENT_USER,
            container_session.container_name,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=_CONTAINER_USER_PREP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            process.kill()
            await self._wait_after_kill(process)
            logger.warning(
                "Timed out preparing non-root user in container %s; continuing with "
                "Claude subprocess watchdog enabled",
                container_session.container_name,
            )
            return
        if process.returncode != 0:
            logger.warning(
                "Failed to prepare non-root user in container %s: %s",
                container_session.container_name,
                output.decode("utf-8", errors="replace").strip(),
            )

    def _write_settings(self, scratch_dir: Path, tool_policy: ToolPolicy) -> None:
        """Write a ``settings.json`` with the bash allowlist policy.

        Always emits the file (even when empty) so ``claude`` sees explicit
        policy rather than falling back to its defaults.
        """
        permissions: dict[str, list[str]] = {
            "allow": [f"Bash({cmd})" for cmd in tool_policy.allowed_bash_commands],
        }
        if tool_policy.disallowed:
            permissions["deny"] = list(tool_policy.disallowed)
        payload = {"permissions": permissions}
        (scratch_dir / "settings.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    async def _exec(
        self,
        *,
        argv: list[str],
        env: dict[str, str],
        cwd: Path,
        transcript_path: Path,
        timeout_seconds: int,
        run_id: UUID,
    ) -> WorkerResult:
        """Invoke ``claude`` and capture its stdout/stderr into ``transcript_path``."""
        start = time.monotonic()
        logger.info("ClaudeCodeWorker invoking %s in %s", argv[0], cwd)

        with transcript_path.open("wb") as transcript:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
            )
            try:
                returncode = await self._wait_with_output_watchdog(
                    process=process,
                    transcript=transcript,
                    start=start,
                    timeout_seconds=timeout_seconds,
                )
            except TimeoutError as e:
                await self._kill_process(process)
                wall = time.monotonic() - start
                summary = str(e) or f"Timed out after {timeout_seconds}s"
                return WorkerResult(
                    run_id=run_id,
                    exit_status="timeout",
                    wall_time_seconds=wall,
                    output_summary=summary,
                    events=tuple(self._events_from_transcript(transcript_path, run_id, wall)),
                )

        wall = time.monotonic() - start
        exit_status: Literal["completed", "failed"] = "completed" if returncode == 0 else "failed"
        summary = self._summarize_transcript(transcript_path)
        events = self._events_from_transcript(transcript_path, run_id, wall)
        return WorkerResult(
            run_id=run_id,
            exit_status=exit_status,
            wall_time_seconds=wall,
            output_summary=summary,
            events=tuple(events),
        )

    async def _wait_with_output_watchdog(
        self,
        *,
        process: asyncio.subprocess.Process,
        transcript: Any,
        start: float,
        timeout_seconds: int,
    ) -> int:
        """Wait for subprocess exit while aborting silent stalls before wall timeout."""
        stdout = process.stdout
        if stdout is None:
            return await asyncio.wait_for(process.wait(), timeout=timeout_seconds)

        hard_deadline = start + timeout_seconds
        idle_deadline = self._next_idle_deadline()
        while True:
            now = time.monotonic()
            wait_seconds = hard_deadline - now
            timeout_message = f"Timed out after {timeout_seconds}s"
            if idle_deadline is not None:
                idle_remaining = idle_deadline - now
                if idle_remaining < wait_seconds:
                    wait_seconds = idle_remaining
                    timeout_message = (
                        "Timed out after "
                        f"{self._inactivity_timeout_seconds}s without Claude output"
                    )
            if wait_seconds <= 0:
                raise TimeoutError(timeout_message)

            try:
                chunk = await asyncio.wait_for(stdout.read(8192), timeout=wait_seconds)
            except TimeoutError as e:
                raise TimeoutError(timeout_message) from e

            if chunk:
                transcript.write(chunk)
                transcript.flush()
                idle_deadline = self._next_idle_deadline()
                continue

            return await asyncio.wait_for(
                process.wait(),
                timeout=max(0.1, hard_deadline - now),
            )

    def _next_idle_deadline(self) -> float | None:
        if self._inactivity_timeout_seconds is None:
            return None
        return time.monotonic() + self._inactivity_timeout_seconds

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await self._wait_after_kill(process)

    async def _wait_after_kill(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_GRACE_SECONDS)
        except TimeoutError:
            logger.exception("ClaudeCodeWorker failed waiting for killed process")

    def _events_from_transcript(
        self,
        transcript_path: Path,
        run_id: UUID,
        wall_time_seconds: float,
    ) -> list[DomainEvent]:
        """Parse Claude Code stream-json into event-store-compatible events."""
        sequencer = EventSequencer(run_id, stream="claude_code")
        events: list[DomainEvent] = []
        try:
            lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return events

        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                events.append(sequencer.thought(line, "output"))
                continue
            if not isinstance(payload, dict):
                continue
            events.extend(self._message_events(payload, sequencer))
            cost_event = self._cost_event(payload, sequencer, wall_time_seconds)
            if cost_event is not None:
                events.append(cost_event)
        return events

    def _message_events(
        self,
        payload: dict[str, object],
        sequencer: EventSequencer,
    ) -> list[DomainEvent]:
        out: list[DomainEvent] = []
        message = payload.get("message")
        blocks: object = None
        if isinstance(message, dict):
            blocks = message.get("content")
        if blocks is None:
            blocks = payload.get("content")
        if not isinstance(blocks, list):
            return out

        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type in {"text", "output"}:
                text = _string_value(block.get("text") or block.get("content"))
                if text:
                    out.append(sequencer.thought(text, "output"))
            elif block_type == "thinking":
                text = _string_value(block.get("thinking") or block.get("text"))
                if text:
                    out.append(sequencer.thought(text, "thinking"))
            elif block_type == "tool_use":
                tool_name = str(block.get("name") or "unknown")
                raw_input = block.get("input")
                tool_input = raw_input if isinstance(raw_input, dict) else {}
                formatted = format_tool_event(tool_name, tool_input)
                detail = json.dumps(tool_input, sort_keys=True, default=str)
                out.append(sequencer.thought(f"{formatted}\nInput: {detail}", "tool_use"))
            elif block_type == "tool_result":
                content = _string_value(block.get("content"))
                out.append(
                    sequencer.thought(
                        f"Tool result: {content or '(no output)'}",
                        "tool_result",
                    )
                )
        return out

    def _cost_event(
        self,
        payload: dict[str, object],
        sequencer: EventSequencer,
        wall_time_seconds: float,
    ) -> DomainEvent | None:
        if payload.get("type") != "result":
            return None
        usage = payload.get("usage")
        usage_dict = usage if isinstance(usage, dict) else {}
        prompt_tokens = _int_value(
            usage_dict.get("input_tokens") or usage_dict.get("prompt_tokens")
        )
        completion_tokens = _int_value(
            usage_dict.get("output_tokens") or usage_dict.get("completion_tokens")
        )
        cache_read_tokens = _int_value(usage_dict.get("cache_read_input_tokens"))
        cache_write_tokens = _int_value(usage_dict.get("cache_creation_input_tokens"))
        reasoning_tokens = _int_value(
            usage_dict.get("reasoning_tokens") or usage_dict.get("thinking_tokens")
        )
        breakdown = UsageBreakdown(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=_float_value(payload.get("total_cost_usd") or payload.get("cost_usd")),
        )
        if not breakdown.has_cost_data:
            return None
        duration = _float_value(payload.get("duration_ms"))
        duration_seconds = (duration / 1000.0) if duration is not None else wall_time_seconds
        model = _string_value(payload.get("model")) or self._model
        return emit_cost(
            sequencer,
            tool_name="claude_code",
            model=model,
            duration_seconds=duration_seconds,
            breakdown=breakdown,
        )

    def _summarize_transcript(self, transcript_path: Path) -> str | None:
        """Best-effort summary of the captured stdout for ``WorkerResult``.

        Reads the last line of stream-json (typically the terminal ``result``
        message). Falls back to ``None`` when parsing fails — the full
        transcript is on disk for downstream tools.
        """
        try:
            text = transcript_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        last_line: str | None = None
        for line in text.splitlines():
            if line.strip():
                last_line = line.strip()
        if last_line is None:
            return None
        try:
            parsed = json.loads(last_line)
        except json.JSONDecodeError:
            return last_line[:500] or None
        if isinstance(parsed, dict):
            for field in ("result", "output", "content", "text"):
                value = parsed.get(field)
                if isinstance(value, str) and value:
                    return value[:500]
        return None


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_string_value(item) for item in value if item is not None)
    if value is None:
        return ""
    return str(value)


def _int_value(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_value(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
