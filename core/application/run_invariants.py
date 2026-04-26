"""Single source of truth for task-level invariants used by both flat and
hierarchical execution modes.

Each builder is pure: (Settings + task context) -> frozen value object. The
dispatcher constructs each once per run and passes them to the WorkerPort,
ensuring flat-mode baselines and hierarchical-mode workers see byte-identical
task framing, tool policy, timeouts, and workspace.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping  # noqa: TC003 — Pydantic resolves field types at runtime.
from pathlib import Path  # noqa: TC003 — Pydantic resolves field types at runtime.
from typing import TYPE_CHECKING, Literal
from uuid import UUID  # noqa: TC003 — Pydantic resolves field types at runtime.

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from config.settings import Settings


# Strict allowlist for subprocess env: anything not listed here is structurally
# unreachable for any subprocess the worker spawns. Matches what we historically
# stripped to before invoking subprocess workers; the centralization here is
# the whole point of this module.
_ENV_ALLOWLIST: frozenset[str] = frozenset(
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


class TaskPromptSpec(BaseModel):
    """Rendered task prompt with provenance for both flat and hierarchical dispatchers.

    Although ``cve_context`` is typed ``Mapping``, Pydantic v2 coerces inputs to a
    plain ``dict`` during validation; mutating its values is not supported and may
    produce undefined behavior. Treat as read-only.
    """

    model_config = {"frozen": True}

    rendered_prompt: str
    briefing_sha: str
    cve_context: Mapping[str, object] | None = None
    task: str


class ToolPolicy(BaseModel):
    """Allowed/disallowed tool names for the worker session."""

    model_config = {"frozen": True}

    allowed: tuple[str, ...]
    disallowed: tuple[str, ...]
    allowed_bash_commands: tuple[str, ...]


class TimeoutBudget(BaseModel):
    """Per-call and per-run wall-clock budgets in seconds."""

    model_config = {"frozen": True}

    per_worker_call: int
    per_run_total: int


class WorkspaceSpec(BaseModel):
    """Generic workspace handoff. Plugin-specific paths live in ``extras``.

    Although ``extras`` is typed ``Mapping``, Pydantic v2 coerces inputs to a plain
    ``dict`` during validation; mutating its values is not supported and may produce
    undefined behavior. Treat as read-only.
    """

    model_config = {"frozen": True}

    root: Path
    extras: Mapping[str, object] = Field(default_factory=dict)


class WorkerResult(BaseModel):
    """Outcome of a single ``WorkerPort.run_task`` invocation."""

    model_config = {"frozen": True}

    run_id: UUID
    exit_status: Literal["completed", "failed", "timeout"]
    tokens_used: int | None = None
    wall_time_seconds: float
    output_summary: str | None = None


def build_task_prompt(
    *,
    briefing_path: Path,
    cve_context: Mapping[str, object] | None,
    task: str,
    cve_context_text: str | None = None,
    cve_context_name: str = "context.json",
) -> TaskPromptSpec:
    """Compose the briefing + CVE context + task slug.

    The briefing is the canonical security-domain framing read from
    ``prompts/domains/secbench/briefing.md``. Returns a frozen spec carrying
    the rendered prompt and the briefing's content sha for provenance.

    The prompt format mirrors what flat-mode dispatch built before
    consolidation, so flat-mode baselines and hierarchical-mode workers see
    byte-identical framing.

    The CVE context can be supplied two ways:

    - ``cve_context_text``: raw text from the fixture file. **When given,
      this is embedded verbatim** — preserves byte-identity regardless of
      fixture formatting (whitespace, key order, trailing newlines).
      Production callers that have the fixture path on disk should pass
      this.
    - ``cve_context``: parsed mapping. Used for in-memory provenance fields
      and as a fallback for embedding when text is not supplied
      (re-serialized via ``json.dumps(parsed, indent=2)``). This path loses
      byte-identity for fixtures whose formatting differs from
      ``json.dumps(indent=2)`` output.

    If both are supplied, ``cve_context_text`` wins for the embedded
    content; ``cve_context`` populates ``TaskPromptSpec.cve_context`` for
    provenance.

    Args:
        briefing_path: Absolute path to the briefing markdown file.
        cve_context: Plugin-supplied structured context. Stored on the
            returned spec for provenance and used as the fallback embed
            source when ``cve_context_text`` is not supplied. ``None``
            (with ``cve_context_text`` also ``None``) skips the context
            block entirely (matching legacy ``context_file=None``).
        task: The task slug provided by the caller.
        cve_context_text: Raw fixture text. When given, embedded verbatim
            for byte-identity with the legacy baseline runner.
        cve_context_name: Filename label for the embedded JSON block. The
            legacy runner uses ``context_file.name``; callers in the new
            architecture pass the original fixture filename when they have
            it, otherwise ``"context.json"`` is the safe default.
    """
    parts: list[str] = []

    briefing_bytes = briefing_path.read_bytes()
    briefing_sha = hashlib.sha256(briefing_bytes).hexdigest()
    # Match legacy ``_compose_prompt``'s ``Path.read_text`` universal-newline
    # behavior so a CRLF-encoded briefing (Windows editor, ``core.autocrlf=true``)
    # produces a prompt byte-identical to the legacy runner. The SHA above is
    # still computed over raw bytes for content-addressed provenance.
    briefing_text = briefing_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if briefing_text:
        parts.append(briefing_text)

    parts.append(f"Task: {task}")

    if cve_context_text is not None:
        # Embed verbatim — preserves byte-identity with the legacy runner
        # regardless of fixture formatting (whitespace, key order, trailing
        # newlines).
        parts.append(
            f"Task context (contents of `{cve_context_name}`):\n```json\n{cve_context_text}\n```"
        )
    elif cve_context is not None:
        # Fallback: re-serialize the parsed mapping. Loses byte-identity
        # for fixtures whose formatting differs from json.dumps(indent=2).
        context_json = json.dumps(dict(cve_context), indent=2)
        parts.append(
            f"Task context (contents of `{cve_context_name}`):\n```json\n{context_json}\n```"
        )

    rendered = "\n\n".join(parts)

    return TaskPromptSpec(
        rendered_prompt=rendered,
        briefing_sha=briefing_sha,
        cve_context=cve_context,
        task=task,
    )


def build_tool_policy(*, settings: Settings) -> ToolPolicy:
    """Extract allowed/disallowed tool names from settings.

    Reads from ``settings.worker.tool_params.<active_tool>`` (the strict
    schema guarantees the active slot is populated). For ``claude_code``
    that's the explicit allowed/disallowed lists. For ``openhands`` and
    ``google_adk`` the allowlist is implicit (all tools allowed); the
    policy reflects that with ``allowed=("*",)`` and ``disallowed=()``.

    ``allowed_bash_commands`` is derived from ``settings.security.tools``
    when security is enabled, else empty.
    """
    tool = settings.worker.tool
    if tool == "claude_code":
        params = settings.worker.tool_params.claude_code
        if params is None:
            raise ValueError(
                "worker.tool_params.claude_code must be populated when worker.tool='claude_code'"
            )
        allowed = tuple(params.allowed_tools)
        disallowed = tuple(params.disallowed_tools)
    else:
        allowed = ("*",)
        disallowed = ()

    allowed_bash = tuple(settings.security.tools) if settings.security.enabled else ()

    return ToolPolicy(
        allowed=allowed,
        disallowed=disallowed,
        allowed_bash_commands=allowed_bash,
    )


def build_timeouts(settings: Settings) -> TimeoutBudget:
    """Extract worker and run timeouts from settings.

    ``worker.timeout`` is the canonical per-worker-call budget. ``openhands``
    also exposes ``tool_params.openhands.timeout_seconds``; that field is
    duplicate today and a follow-up PR will collapse them.

    Per-run total comes from ``orchestration.max_run_duration_seconds``,
    which is float-typed in settings; we coerce to int seconds here so the
    budget value object matches ``per_worker_call``'s type.
    """
    return TimeoutBudget(
        per_worker_call=int(settings.worker.timeout),
        per_run_total=int(settings.orchestration.max_run_duration_seconds),
    )


def build_env_policy() -> frozenset[str]:
    """Return the env-var allowlist for subprocess execution.

    Mirrors the explicit allowlist in the legacy baseline runner's
    ``_build_subprocess_env``. Anything not listed here is structurally
    unreachable for any subprocess the worker spawns — POSTGRES_PASSWORD,
    OPENAI_API_KEY, ARISE_*, database URLs, etc. are all stripped.
    """
    return _ENV_ALLOWLIST


def build_workspace_spec(
    *,
    run_dir: Path,
    extras: Mapping[str, object] | None = None,
) -> WorkspaceSpec:
    """Construct the workspace handoff for a run.

    ``core/`` is domain-agnostic; the actual workspace prep (e.g. mounting
    /src and /testcase, ensuring ``secb`` is available) is the security
    plugin's job and is invoked separately. This builder only stamps the
    root path and any extras the plugin populates.
    """
    return WorkspaceSpec(root=run_dir, extras=dict(extras or {}))
