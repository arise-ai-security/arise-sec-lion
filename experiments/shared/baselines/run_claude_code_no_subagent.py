"""Baseline variant: Claude Code with the Task (subagent) tool disabled.

Identical to ``run_claude_code`` except that ``--disallowedTools Task``
prevents Claude from spawning subagents. Surfaces the cost/quality
difference between flat and hierarchical execution when paired with the
subagent-enabled variant.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

from experiments.shared.baselines.run_claude_code import (
    _build_subprocess_env,
    _compose_prompt,
    build_command,
)


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

VARIANT_NAME = "claude-code-nosubagent"


def run_baseline(
    *,
    run_dir: Path,
    task_prompt: str,
    context_file: Path | None = None,
    timeout_seconds: int = 1800,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Invoke Claude Code without the Task tool, capturing transcript."""
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise RuntimeError(
            "`claude` CLI not found on PATH; install Claude Code before running baselines"
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / "stdout_stderr.log"

    prompt = _compose_prompt(task_prompt=task_prompt, context_file=context_file)
    cmd = build_command(
        claude_bin=claude_bin,
        task_prompt=prompt,
        extra_args=["--disallowedTools", "Task"],
    )

    logger.info("running baseline variant=%s in %s", VARIANT_NAME, run_dir)
    with transcript.open("wb") as log_file:
        try:
            completed = subprocess.run(  # noqa: S603
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                env=_build_subprocess_env(env),
                cwd=run_dir,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"variant": VARIANT_NAME, "exit_status": "timeout"}

    exit_status = "success" if completed.returncode == 0 else "failed"
    return {"variant": VARIANT_NAME, "exit_status": exit_status}
