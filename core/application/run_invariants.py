"""Single source of truth for task-level invariants used by both flat and
hierarchical execution modes.

Each builder is pure: (Settings + task context) -> frozen value object. The
dispatcher constructs each once per run and passes them to the WorkerPort,
ensuring flat-mode baselines and hierarchical-mode workers see byte-identical
task framing, tool policy, timeouts, and workspace.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from core.domain.events.events import DomainEvent


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
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)


class TaskPromptSpec(BaseModel):
    """Rendered task prompt with provenance for both flat and hierarchical dispatchers.

    Although ``domain_context`` is typed ``Mapping``, Pydantic v2 coerces inputs to a
    plain ``dict`` during validation; mutating its values is not supported and may
    produce undefined behavior. Treat as read-only.

    ``prompt_sha`` is the sha256 hex digest of ``rendered_prompt`` itself — a
    content-addressed fingerprint for run-level provenance. (The historical
    ``briefing_sha`` field, which hashed ``briefing.md``, was retired when the
    flat prompt switched to the unified ``inputs/*`` + ``system/*`` template chain.)
    """

    model_config = {"frozen": True}

    rendered_prompt: str
    prompt_sha: str
    domain_context: Mapping[str, object] | None = None
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
    events: tuple[DomainEvent, ...] = ()


def build_timeouts(settings: Settings) -> TimeoutBudget:
    """Extract worker and run timeouts from settings.

    ``worker.timeout`` is the canonical per-worker-call budget.
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

    ``core/`` is domain-agnostic; the plugin performs any domain-specific
    workspace preparation separately. This builder only stamps the root path
    and any extras the plugin populates.
    """
    return WorkspaceSpec(root=run_dir, extras=dict(extras or {}))
