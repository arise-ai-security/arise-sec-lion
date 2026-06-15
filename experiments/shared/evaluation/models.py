"""Value objects and result models for run evaluation.

Result models are frozen Pydantic for validation/serialization. ``RunData`` is a
frozen dataclass because it carries a (large) live list of deserialized events
that should not be deep-copied by Pydantic on every construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — runtime-required by Pydantic field resolution

from pydantic import BaseModel


if TYPE_CHECKING:
    from pathlib import Path

    from core.domain.events.events import DomainEvent


class BefPhase(str, Enum):
    """Top-level BEF phase a leaf agent belongs to.

    ``ORCHESTRATION`` buckets boss/manager work that is not under any phase
    subtree. ``LINEAR`` is used by the N1/N2 functions, where one agent runs all
    phases and no per-phase agent exists.
    """

    BUILDER = "Builder"
    EXPLOITER = "Exploiter"
    FIXER = "Fixer"
    REPORTER = "Reporter"
    ORCHESTRATION = "orchestration"
    LINEAR = "linear"


class ToolCategory(str, Enum):
    """Tool-call category, grounded in the config-defined worker tool list.

    ``file_editor`` splits into FILE_READ/FILE_WRITE by its ``command``;
    ``shell_in_container`` maps to BASH. The config-defined security tools
    (``valgrind``, ``klee`` — see ``secbench.tools`` / the ``valgrind_run`` and
    ``klee_run`` MCP tools) are invoked via shell in practice, so a shell call
    that runs one is bucketed separately as VALGRIND/KLEE. A non-shell MCP action
    falls back to ``MCP_OTHER``. ``FINISH`` is the agent's terminal control signal
    (excluded from tool-call totals).
    """

    FILE_READ = "FileRead"
    FILE_WRITE = "FileWrite"
    GREP = "Grep"
    GLOB = "Glob"
    BASH = "Bash"
    VALGRIND = "Valgrind"
    KLEE = "KLEE"
    MCP_OTHER = "MCPOther"
    FINISH = "Finish"
    OTHER = "Other"


@dataclass(frozen=True, slots=True)
class CveOracle:
    """Host-side CVE ground truth for offline judging, projected for the eval layer.

    A raw (plugin-free) projection of the host dataset JSON — the *same* file the
    harness resolved to launch the run. Carries only the judging fields verbatim:
    the expected-failure oracle (``sanitizer`` / ``sanitizer_report`` /
    ``bug_report`` / ``bug_description``) that is already rendered to the agent,
    plus the optional host-side secret ``gold_patch`` consumed *only* by the
    patch-correctness judge. ``gold_patch`` must NEVER feed a prompt (mirrors the
    plugin's ``_PROMPT_FORBIDDEN_FIELDS``).

    Every field is a verbatim copy of the dataset JSON — no semantic derivation
    (no expected-error class, no crash-frame extraction). All semantic judgement
    is deferred to the LLM judges, which are fed these raw bytes.
    """

    instance_id: str
    sanitizer: str
    sanitizer_report: str
    bug_report: str
    bug_description: str
    base_commit: str = ""
    gold_patch: str | None = None


@dataclass(frozen=True, slots=True)
class RunData:
    """Everything an evaluation function needs for one run.

    ``events`` is the flat, time-ordered event stream for the whole agent
    hierarchy (boss + all descendants). ``run_dir`` is ``runs/<run_id>/`` on
    disk. ``manifest`` is the parsed ``run_manifest.json`` (may be empty if
    absent). ``cve`` is the optional host-side ground-truth oracle threaded in at
    the loader/harness layer (``None`` when unavailable — judges degrade to
    mechanical-only).
    """

    run_id: UUID
    events: list[DomainEvent]
    run_dir: Path
    manifest: dict[str, Any]
    cve: CveOracle | None = None


class CostByRole(BaseModel):
    """Cost in USD split across node roles.

    ``by_role`` always contains all four role keys (BOSS/MANAGER/WORKER/PENDING)
    plus ``UNKNOWN`` when an agent's role cannot be resolved. The values sum to
    ``total_usd`` within rounding tolerance (enforced by the caller).
    """

    model_config = {"frozen": True}

    total_usd: float
    llm_usd: float
    worker_usd: float
    by_role: dict[str, float]


class CountBreakdown(BaseModel):
    """A total count plus its breakdown across a keyspace.

    Used for tool-call counts grouped by BEF phase, by tool category, or by node
    role. ``by`` keys are the relevant enum/role string values; ``total`` is the
    sum of ``by`` values.
    """

    model_config = {"frozen": True}

    total: int
    by: dict[str, int]


class RateBreakdown(BaseModel):
    """Cache-hit rates across a keyspace.

    ``by`` maps each group to ``cache_read / prompt_tokens`` in [0, 1], or
    ``None`` when that group has zero prompt tokens (rate undefined).
    ``overall`` is the pooled rate across all groups (``None`` if no prompt
    tokens anywhere).
    """

    model_config = {"frozen": True}

    overall: float | None
    by: dict[str, float | None]


class ToolCall(BaseModel):
    """A single worker tool invocation reconstructed from a ThoughtCaptured event."""

    model_config = {"frozen": True}

    agent_id: UUID
    sequence_number: int
    action: str  # OpenHands action class, e.g. "FileEditorAction"
    category: ToolCategory
    command: str | None = None  # parsed file_editor command or shell command, if any


class WorkerPrompt(BaseModel):
    """One worker-execution prompt with its provenance."""

    model_config = {"frozen": True}

    agent_id: UUID
    phase: BefPhase
    role_label: str  # bracket role, e.g. "PoC-Researcher" (or "-" if none)
    sequence_number: int
    target: str  # tool name, e.g. "openhands"
    prompt: str


class WorkerPrompts(BaseModel):
    """All worker-execution prompts for a run, plus a prettified concatenation."""

    model_config = {"frozen": True}

    prompts: list[WorkerPrompt]
    text: str


class ArtifactRef(BaseModel):
    """A non-vacuous file written/edited by an agent.

    ``path`` is the container path recorded in the event (e.g.
    ``/testcase/model_patch.diff``); ``disk_path`` is its resolved location under
    ``runs/<run_id>/``.
    """

    model_config = {"frozen": True}

    path: str
    disk_path: str
    size_bytes: int
    edited_by: UUID


class ArtifactsBySubtree(BaseModel):
    """Non-vacuous artifacts written/edited, grouped by BEF subtree.

    ``by`` maps a :class:`BefPhase` value to the files that subtree's agents wrote.
    """

    model_config = {"frozen": True}

    by: dict[str, list[ArtifactRef]]
