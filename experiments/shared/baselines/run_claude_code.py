"""Baseline: invoke Claude Code non-interactively with subagents enabled.

Spec §8 names this variant ``claude-code-subagent`` — it's the "full" Claude
Code baseline that freely dispatches to Task/Agent subagents during its run.
Output is streamed as JSON Lines via ``--output-format stream-json`` so
downstream analysis has structured events; stdout+stderr are also captured
into ``stdout_stderr.log`` verbatim for inspection.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

VARIANT_NAME = "claude-code-subagent"

# Strict allowlist for subprocess env. Anything not listed here is stripped so
# the baseline subprocess cannot read our Postgres password, OpenAI key, etc.
# ANTHROPIC_API_KEY is included because `claude` needs it to authenticate.
_SUBPROCESS_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Shell/locale basics.
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Claude Code authentication (baselines invoke Claude directly).
        "ANTHROPIC_API_KEY",
    }
)


def _build_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the env dict for a baseline subprocess.

    Starts from an EMPTY dict, copies only the allowlisted keys from
    ``os.environ``, then layers caller-supplied ``extra`` on top so tests can
    override any key deterministically. Anything outside the allowlist
    (POSTGRES_*, OPENAI_API_KEY, LLM_API_KEY, ARISE_*, database URLs, …) is
    structurally unreachable for the subprocess.
    """
    env: dict[str, str] = {}
    for key in _SUBPROCESS_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if extra:
        env.update(extra)
    return env


def build_command(
    *,
    claude_bin: str,
    task_prompt: str,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Return the argv list for Claude Code. Factored out for testability."""
    args = [
        claude_bin,
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
    ]
    if extra_args:
        args.extend(extra_args)
    args.append(task_prompt)
    return args


def run_baseline(
    *,
    run_dir: Path,
    task_prompt: str,
    context_file: Path | None = None,
    timeout_seconds: int = 1800,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Invoke Claude Code and capture its full output into the run directory.

    Returns a dict of extras to merge into the baseline's run_manifest.json
    (notably the exit status classification and invocation metadata).
    The caller writes the manifest itself.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise RuntimeError(
            "`claude` CLI not found on PATH; install Claude Code before running baselines"
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / "stdout_stderr.log"

    # Include the domain-context JSON inline in the prompt so the baseline has
    # the same fixture Bootstrap would have passed through --domain-context-file.
    prompt = _compose_prompt(task_prompt=task_prompt, context_file=context_file)
    cmd = build_command(claude_bin=claude_bin, task_prompt=prompt)

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


def _compose_prompt(
    *,
    task_prompt: str,
    context_file: Path | None,
    briefing_path: Path | None = None,
) -> str:
    """Compose the baseline prompt.

    The prompt bundles (1) the repo's shared SEC-bench briefing so the baseline
    measures the same task `main.py run` describes to its boss agent, (2) the
    context-file JSON verbatim so the baseline has the same fixture, and
    (3) the task slug the caller supplied.
    """
    parts: list[str] = []
    briefing = _read_briefing(briefing_path)
    if briefing:
        parts.append(briefing)

    parts.append(f"Task: {task_prompt}")

    if context_file is not None:
        try:
            context = context_file.read_text(encoding="utf-8")
        except OSError:
            context = None
        if context:
            parts.append(
                f"Task context (contents of `{context_file.name}`):\n"
                f"```json\n{context}\n```"
            )
    return "\n\n".join(parts)


def _read_briefing(briefing_path: Path | None) -> str | None:
    """Return the briefing text, checking the migrated and legacy locations."""
    from experiments.shared.scripts._paths import get_repo_root

    candidates: list[Path] = []
    if briefing_path is not None:
        candidates.append(briefing_path)
    root = get_repo_root()
    candidates.extend(
        [
            root / "prompts" / "domains" / "secbench" / "briefing.md",
            root / "experiments" / "configs" / "domain_briefing.md",
        ]
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return None
