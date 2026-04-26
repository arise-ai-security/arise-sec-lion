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
from typing import TYPE_CHECKING

from core.application.run_invariants import (
    WorkerResult,
    build_env_policy,
)
from core.domain.exceptions import ToolNotAvailableError


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


class ClaudeCodeWorker:
    """Adapter satisfying ``WorkerPort`` by invoking the ``claude`` CLI.

    A scratch ``CLAUDE_CONFIG_DIR`` is materialized per call so the CLI reads
    only the policy our ``ToolPolicy`` declares, never the operator's
    ``~/.claude/settings.json``.
    """

    def __init__(
        self,
        *,
        output_format: str = _DEFAULT_OUTPUT_FORMAT,
        include_partial_messages: bool = True,
        env_allowlist: frozenset[str] | None = None,
    ) -> None:
        self._output_format = output_format
        self._include_partial_messages = include_partial_messages
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
        claude_bin = shutil.which("claude")
        if claude_bin is None:
            raise ToolNotAvailableError(
                tool_name="claude",
                available_tools=[],
            )

        run_dir = Path(workspace.root)
        run_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = run_dir / "stdout_stderr.log"

        with tempfile.TemporaryDirectory(prefix="claude-cfg-") as scratch:
            scratch_dir = Path(scratch)
            self._write_settings(scratch_dir, tool_policy)
            argv = self._build_argv(
                claude_bin=claude_bin,
                rendered_prompt=spec.rendered_prompt,
                tool_policy=tool_policy,
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

    def _build_argv(
        self,
        *,
        claude_bin: str,
        rendered_prompt: str,
        tool_policy: ToolPolicy,
    ) -> list[str]:
        argv = [
            claude_bin,
            "-p",
            "--output-format",
            self._output_format,
        ]
        if self._include_partial_messages:
            argv.append("--include-partial-messages")
        for tool in tool_policy.disallowed:
            argv.extend(["--disallowedTools", tool])
        argv.append(rendered_prompt)
        return argv

    def _build_env(self, *, scratch_dir: Path) -> dict[str, str]:
        """Allowlist-only env, plus a scratch ``CLAUDE_CONFIG_DIR``."""
        env: dict[str, str] = {}
        for key in self._env_allowlist:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        env["CLAUDE_CONFIG_DIR"] = str(scratch_dir)
        return env

    def _write_settings(self, scratch_dir: Path, tool_policy: ToolPolicy) -> None:
        """Write a ``settings.json`` with the bash allowlist policy.

        Always emits the file (even when empty) so ``claude`` sees explicit
        policy rather than falling back to its defaults.
        """
        permissions = {
            "allow": [f"Bash({cmd})" for cmd in tool_policy.allowed_bash_commands],
        }
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
                )

        wall = time.monotonic() - start
        exit_status = "completed" if returncode == 0 else "failed"
        summary = self._summarize_transcript(transcript_path)
        return WorkerResult(
            run_id=run_id,
            exit_status=exit_status,
            wall_time_seconds=wall,
            output_summary=summary,
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
