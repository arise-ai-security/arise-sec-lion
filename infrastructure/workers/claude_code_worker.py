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
from core.domain.events.events import DomainEvent
from core.domain.exceptions import ToolNotAvailableError
from infrastructure.adapters.worker.shared import (
    ContainerSessionContext,
    build_claude_container_env,
    build_docker_exec_argv,
    prepare_claude_container_user,
    to_cli_config_payload,
    to_in_container_mcp_servers,
)
from infrastructure.io.atomic_write import write_json
from infrastructure.workers.claude_transcript import (
    cost_event,
    events_from_transcript,
    summarize_transcript,
)


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkspaceSpec,
    )
    from infrastructure.adapters.worker.shared import EventSequencer


logger = logging.getLogger(__name__)


_DEFAULT_OUTPUT_FORMAT = "stream-json"
_DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 600
_PROCESS_KILL_GRACE_SECONDS = 5


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
        """Run ``claude`` inside the plugin-provided container.

        ``mcp_servers`` from ``workspace.extras`` is rewritten for in-container
        execution: the JSON config and stdio command are remapped so the MCP
        server runs inside the same container as the agent without a host-side
        helper hop.
        """
        # Inside the container, ``claude`` is on PATH (installed by the
        # container image); no host shutil.which probe.
        in_container_claude = "claude"
        await prepare_claude_container_user(container_session)
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

        The host-side stdio config can provide an ``in_container`` override.
        When ``claude`` runs inside the container, that metadata supplies the
        executable, arguments, and environment rewrite.

        The JSON file lands in the bind-mounted run directory so it is visible
        from both host and container; the returned path is the *container*
        path so ``--mcp-config`` resolves correctly inside the container.
        """
        servers = workspace.extras.get("mcp_servers")
        if not isinstance(servers, dict) or not servers:
            return None
        rewritten = to_in_container_mcp_servers(servers)
        if not rewritten:
            return None
        host_target = run_dir / "mcp_servers.in_container.json"
        write_json(host_target, to_cli_config_payload(rewritten))
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
        # as root, and plugin containers may run as root by default.
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
        from the host — the container has its own coherent base
        environment and forwarding host values would override container PATH
        and break ``claude`` resolution.
        """
        return build_claude_container_env(scratch_container_dir=scratch_container_dir)

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
        container image may inherit root from its base, so we drop privileges
        only for this exec by passing ``--user 1000:1000`` after preparing a
        matching passwd/group/home entry in the running container. The
        bind-mounted source and artifact trees are assumed to be left
        world-readable/writable by the domain plugin's workspace preparation.
        """
        return build_docker_exec_argv(
            container_session=container_session,
            command_argv=claude_argv,
            env=env,
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
                return WorkerResult(
                    run_id=run_id,
                    exit_status="timeout",
                    wall_time_seconds=wall,
                    output_summary=str(e) or f"Timed out after {timeout_seconds}s",
                    events=tuple(self._events_from_transcript(transcript_path, run_id, wall)),
                )

        wall = time.monotonic() - start
        exit_status: Literal["completed", "failed"] = "completed" if returncode == 0 else "failed"
        summary: str | None = summarize_transcript(transcript_path)
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
                        f"Timed out after {self._inactivity_timeout_seconds}s without Claude output"
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
        return events_from_transcript(transcript_path, run_id, wall_time_seconds, self._model)

    def _cost_event(
        self,
        payload: dict[str, object],
        sequencer: EventSequencer,
        wall_time_seconds: float,
    ) -> DomainEvent | None:
        return cost_event(payload, sequencer, wall_time_seconds, self._model)
