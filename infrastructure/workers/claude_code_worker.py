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
    ) -> None:
        self._model = model
        self._output_format = output_format
        self._include_partial_messages = include_partial_messages
        self._max_turns = max_turns
        self._use_global_config = use_global_config
        self._env_allowlist = env_allowlist or build_env_policy()

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
        secb-tools image inherits root from the SEC-bench base; rather than
        adding a user directive to the Dockerfile, we drop privileges only for
        this exec by passing ``--user 1000:1000``. The bind-mounted source and
        testcase trees are already world-readable/writable
        (``DockerSecBenchRuntime._copy_source_tree`` chmods them 0o777/0o666),
        and ``secb-exec`` runs as a *separate* docker exec from the host that
        defaults back to root, so build/test commands keep their privileges.
        """
        wrapped: list[str] = ["docker", "exec", "-i"]
        wrapped.extend(["--user", "1000:1000"])
        wrapped.extend(["-w", container_session.container_working_directory])
        for key, value in env.items():
            wrapped.extend(["-e", f"{key}={value}"])
        wrapped.append(container_session.container_id)
        wrapped.extend(claude_argv)
        return wrapped

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
                stdout=transcript,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
            )
            try:
                returncode = await asyncio.wait_for(
                    process.wait(),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    logger.exception("ClaudeCodeWorker failed waiting for killed process")
                wall = time.monotonic() - start
                return WorkerResult(
                    run_id=run_id,
                    exit_status="timeout",
                    wall_time_seconds=wall,
                    output_summary=f"Timed out after {timeout_seconds}s",
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
