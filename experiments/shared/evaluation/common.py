"""Pure helpers shared across evaluation functions.

Everything here operates on an in-memory ``list[DomainEvent]`` (and, for artifact
checks, a run directory) — no database access — so it is fully unit-testable.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.events.events import (
    AgentCreated,
    AgentExecutionFinished,
    AgentExecutionStarted,
    ComplexityEvaluated,
    DomainEvent,
    OperationStarted,
    TaskAssigned,
    ThoughtCaptured,
)
from core.domain.values.enums import AgentRole
from experiments.shared.evaluation.models import BefPhase, ToolCall, ToolCategory


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


logger = logging.getLogger(__name__)

WORKER_EXECUTION = "worker_execution"
UNKNOWN_ROLE = "UNKNOWN"

_BRACKET_PREFIX = re.compile(r"^\s*\[([^\]]+)\]")
_FILE_WRITE_COMMANDS = frozenset({"create", "str_replace", "insert", "undo_edit"})
# An MCPToolAction's content carries a Python repr "data={...} kind='MCPToolAction'";
# this captures the data dict so it can be parsed with ast.literal_eval.
_MCP_DATA = re.compile(r"data=(\{.*\})\s+kind=", re.DOTALL)

# Config-defined security tools (config secbench.tools / the valgrind_run & klee_run
# MCP tools). They are invoked via shell, so a shell call is bucketed as one only when
# the tool is the actual *command head* of a segment (not an argument like
# ``apt-get install valgrind`` / ``which valgrind``, nor heredoc content like a
# packages.txt listing, nor a filename like ``fix_valgrind.log``).
_SECURITY_TOOLS: dict[str, ToolCategory] = {
    "valgrind": ToolCategory.VALGRIND,
    "klee": ToolCategory.KLEE,
}
# Strip heredoc bodies (``<<EOF ... EOF``) so their lines are not parsed as commands.
_HEREDOC = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?.*?\n\1\b", re.DOTALL)
# Split a command into segments on shell operators (; && || | newline ( ) ` $( ).
_SEGMENT_SPLIT = re.compile(r"&&|\|\||\$\(|[;\n|()`]")
# A leading ``NAME=value`` environment-variable assignment prefix.
_ENV_PREFIX = re.compile(r"\w+=\S*\s+")

# Finer BEF leaf-role names → their phase. Keep local to evaluation so this
# generic analysis package does not import the security plugin.
_PHASE_BY_LEAF_ROLE: dict[str, BefPhase] = {
    "instrumented-builder": BefPhase.BUILDER,  # legacy label
    "build-setup": BefPhase.BUILDER,
    "build-compiler": BefPhase.BUILDER,
    "build-verifier": BefPhase.BUILDER,
    "poc-researcher": BefPhase.EXPLOITER,
    "data-flow-analyst": BefPhase.EXPLOITER,
    "poc-tester": BefPhase.EXPLOITER,
    "forward-instrumentator": BefPhase.EXPLOITER,
    "repro-creator": BefPhase.EXPLOITER,
    "exploit-validator": BefPhase.EXPLOITER,
    "root-cause-analyst": BefPhase.FIXER,
    "candidate-reviewer": BefPhase.FIXER,
    "regression-tester": BefPhase.FIXER,
    "patch-creator": BefPhase.FIXER,
    "patch-validator": BefPhase.FIXER,
    "fix-aggregator": BefPhase.FIXER,
    "reporter": BefPhase.REPORTER,
}


# ---------------------------------------------------------------------------
# Event indexing
# ---------------------------------------------------------------------------


def events_of_type[T: DomainEvent](
    events: Iterable[DomainEvent], event_type: type[T]
) -> Iterator[T]:
    """Yield events that are instances of ``event_type``."""
    for event in events:
        if isinstance(event, event_type):
            yield event


def first_per_aggregate[T: DomainEvent](
    events: Iterable[DomainEvent], event_type: type[T]
) -> dict[UUID, T]:
    """Map each aggregate_id to its first (lowest-sequence) event of a type."""
    result: dict[UUID, T] = {}
    for event in events_of_type(events, event_type):
        existing = result.get(event.aggregate_id)
        if existing is None or event.sequence_number < existing.sequence_number:
            result[event.aggregate_id] = event
    return result


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def build_role_map(events: Iterable[DomainEvent]) -> dict[UUID, str]:
    """Map aggregate_id → resolved uppercased role ("BOSS"/"MANAGER"/"WORKER"/"PENDING").

    ``AgentCreated.role`` is the *initial* role: spawned agents start as ``pending``
    and only get their real role (manager/worker) once assessment runs. So the
    resolved role per aggregate is, in priority order:

    1. ``BOSS`` if created as boss (the root stays boss even when it runs worker
       execution in flat N1/N2 mode).
    2. ``ComplexityEvaluated.determined_role`` (the assess decision: manager/worker).
    3. The execution role (``AgentExecution{Started,Finished}.role``).
    4. The initial ``AgentCreated.role`` (e.g. ``pending`` if never assessed).

    Agents with no ``AgentCreated`` are absent (callers default them to ``UNKNOWN``).
    """
    events = list(events)
    created = {e.aggregate_id: (e.role or "").lower() for e in events_of_type(events, AgentCreated)}
    determined = {
        e.aggregate_id: (e.determined_role or "").lower()
        for e in events_of_type(events, ComplexityEvaluated)
        if e.determined_role
    }
    executed: dict[UUID, str] = {}
    for event in events:
        if isinstance(event, (AgentExecutionStarted, AgentExecutionFinished)) and event.role:
            executed.setdefault(event.aggregate_id, event.role.lower())

    role_map: dict[UUID, str] = {}
    for aggregate_id, initial in created.items():
        if initial == AgentRole.BOSS.value:
            resolved = AgentRole.BOSS.value
        else:
            resolved = determined.get(aggregate_id) or executed.get(aggregate_id) or initial
        role_map[aggregate_id] = resolved.upper() or UNKNOWN_ROLE
    return role_map


def role_key(role_map: dict[UUID, str], agent_id: UUID) -> str:
    """Resolve an agent's role key, defaulting to ``UNKNOWN``."""
    return role_map.get(agent_id, UNKNOWN_ROLE)


# ---------------------------------------------------------------------------
# BEF phase mapping
# ---------------------------------------------------------------------------


def bracket_prefix(text: str | None) -> str | None:
    """Extract the leading ``[Bracket]`` role label, if any."""
    if not text:
        return None
    match = _BRACKET_PREFIX.match(text)
    return match.group(1).strip() if match else None


def classify_phase(label: str | None) -> BefPhase | None:
    """Classify a bracket label into a top-level BEF phase.

    Only the four exact phase names and the known finer leaf-role names are
    accepted (no loose keyword guessing — real phase nodes always carry an exact
    ``[Builder]``/``[Exploiter]``/``[Fixer]``/``[Reporter]`` bracket). Returns
    ``None`` when nothing matches, which buckets the agent into ``ORCHESTRATION``.
    """
    if not label:
        return None
    normalized = label.strip().lower()
    for phase in (BefPhase.BUILDER, BefPhase.EXPLOITER, BefPhase.FIXER, BefPhase.REPORTER):
        if normalized == phase.value.lower():
            return phase
    return _PHASE_BY_LEAF_ROLE.get(normalized)


def build_parent_map(events: Iterable[DomainEvent]) -> dict[UUID, UUID | None]:
    """Map aggregate_id → parent_id from ``AgentCreated`` events."""
    return {c.aggregate_id: c.parent_id for c in events_of_type(events, AgentCreated)}


def build_task_description_map(events: Iterable[DomainEvent]) -> dict[UUID, str]:
    """Map aggregate_id → its first assigned task description."""
    return {
        agg: assigned.task_description
        for agg, assigned in first_per_aggregate(events, TaskAssigned).items()
    }


def build_bef_phase_map(events: Iterable[DomainEvent], root_id: UUID) -> dict[UUID, BefPhase]:
    """Map every agent to the BEF subtree it belongs to.

    An agent's phase is the phase of its ancestor that is a *direct child of the
    boss* (the phase node). The boss and any agent whose phase node cannot be
    classified fall into ``ORCHESTRATION``.
    """
    events = list(events)
    parent_map = build_parent_map(events)
    task_map = build_task_description_map(events)

    def phase_node_label(agent_id: UUID) -> str | None:
        """Walk up to the direct boss-child and return its bracket label."""
        seen: set[UUID] = set()
        cursor: UUID | None = agent_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            parent = parent_map.get(cursor)
            if parent == root_id:
                return bracket_prefix(task_map.get(cursor))
            if parent is None:
                return None  # reached a root that is not under the boss
            cursor = parent
        return None

    phase_map: dict[UUID, BefPhase] = {}
    for agent_id in parent_map:
        if agent_id == root_id:
            phase_map[agent_id] = BefPhase.ORCHESTRATION
            continue
        phase = classify_phase(phase_node_label(agent_id))
        phase_map[agent_id] = phase if phase is not None else BefPhase.ORCHESTRATION
    return phase_map


# ---------------------------------------------------------------------------
# Worker-execution agents
# ---------------------------------------------------------------------------


def worker_execution_agents(events: Iterable[DomainEvent]) -> set[UUID]:
    """Aggregate ids that ran a ``worker_execution`` operation."""
    return {
        op.aggregate_id
        for op in events_of_type(events, OperationStarted)
        if op.operation_type == WORKER_EXECUTION
    }


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def _parse_tool_input(content: str) -> dict[str, object]:
    """Parse the ``Input: {json}`` payload from a ThoughtCaptured content string."""
    marker = "\nInput: "
    idx = content.find(marker)
    if idx == -1:
        return {}
    try:
        parsed = json.loads(content[idx + len(marker) :])
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _file_editor_category(payload: dict[str, object]) -> tuple[ToolCategory, str | None]:
    """Categorize a FileEditorAction by its ``command`` (view → read, else write)."""
    command = payload.get("command")
    command = command if isinstance(command, str) else None
    if command == "view":
        return ToolCategory.FILE_READ, command
    if command in _FILE_WRITE_COMMANDS:
        return ToolCategory.FILE_WRITE, command
    logger.debug("Unrecognized file_editor command %r", command)
    return ToolCategory.OTHER, command


def _mcp_shell_command(payload: dict[str, object]) -> tuple[bool, str | None]:
    """Decide whether an MCPToolAction is a shell call and recover its command.

    ``shell_in_container`` is the only command-bearing MCP tool, so a ``command``
    key reliably means a shell (Bash) call. The command is parsed from the nested
    ``data={...}`` repr with ``ast.literal_eval`` (robust to quotes/braces); a
    truncated repr falls back to the ``'command'`` substring signal.

    Returns ``(is_shell, command)``.
    """
    direct = payload.get("command")
    if isinstance(direct, str):
        return True, direct
    repr_str = payload.get("repr")
    if not isinstance(repr_str, str):
        return False, None
    match = _MCP_DATA.search(repr_str)
    if match:
        try:
            data = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            data = None
        if isinstance(data, dict):
            command = data.get("command")
            return (True, command) if isinstance(command, str) else (False, None)
    # Unparseable (e.g. producer-truncated repr): trust the command substring signal.
    return ("'command'" in repr_str), None


def _command_heads(command: str) -> Iterator[str]:
    """Yield the head (command) token of each shell segment, env-prefix stripped."""
    for segment in _SEGMENT_SPLIT.split(_HEREDOC.sub("", command)):
        stripped = segment.strip()
        while (prefix := _ENV_PREFIX.match(stripped)) is not None:
            stripped = stripped[prefix.end() :].lstrip()
        if stripped:
            yield stripped.split(None, 1)[0]


def _security_tool(command: str) -> ToolCategory | None:
    """Return the security-tool category a shell command directly invokes.

    A tool counts only when it is the command head of a segment (basename match),
    so arguments (``apt-get install valgrind``, ``which valgrind``), heredoc
    content, and filename mentions are excluded.
    """
    for head in _command_heads(command):
        category = _SECURITY_TOOLS.get(PurePosixPath(head).name)
        if category is not None:
            return category
    return None


def _mcp_category(payload: dict[str, object]) -> tuple[ToolCategory, str | None]:
    """Categorize an MCPToolAction: shell (Bash / security tool) vs other MCP."""
    is_shell, command = _mcp_shell_command(payload)
    if not is_shell:
        return ToolCategory.MCP_OTHER, None
    security = _security_tool(command) if command else None
    return (security, command) if security else (ToolCategory.BASH, command)


def _categorize(action: str, content: str) -> tuple[ToolCategory, str | None]:
    """Map an OpenHands action + content to a tool category and command, if any."""
    payload = _parse_tool_input(content)
    if action == "FileEditorAction":
        return _file_editor_category(payload)
    if action == "GrepAction":
        return ToolCategory.GREP, None
    if action == "GlobAction":
        return ToolCategory.GLOB, None
    if action == "FinishAction":
        return ToolCategory.FINISH, None
    if action == "MCPToolAction":
        return _mcp_category(payload)
    logger.debug("Unrecognized tool action %r", action)
    return ToolCategory.OTHER, None


def iter_tool_calls(events: Iterable[DomainEvent]) -> Iterator[ToolCall]:
    """Yield one ToolCall per genuine tool invocation.

    A tool call is a ``ThoughtCaptured`` with ``output_type == "tool_use"`` and a
    non-empty ``tool_name``. ``tool_name == None`` rows are agent *thoughts* and
    ``tool_result`` rows are responses — both excluded.
    """
    for tc in events_of_type(events, ThoughtCaptured):
        if tc.output_type != "tool_use":
            continue
        action = (tc.tool_name or "").strip()
        if not action:
            continue
        category, command = _categorize(action, tc.content)
        yield ToolCall(
            agent_id=tc.aggregate_id,
            sequence_number=tc.sequence_number,
            action=action,
            category=category,
            command=command,
        )


def is_counted_tool_call(call: ToolCall) -> bool:
    """Whether a tool call counts toward the total (excludes the Finish signal)."""
    return call.category is not ToolCategory.FINISH


# ---------------------------------------------------------------------------
# Filesystem (artifacts)
# ---------------------------------------------------------------------------


def container_path_to_disk(path: str, run_dir: Path) -> Path | None:
    """Map a container path (``/testcase/...``, ``/src/...``) to its disk location.

    Returns ``None`` for paths outside the two mounted roots.
    """
    if not path:
        return None
    pure = PurePosixPath(path)
    parts = pure.parts
    # Skip a leading "/" part so parts[0] is the mount name.
    if parts and parts[0] == "/":
        parts = parts[1:]
    if not parts or parts[0] not in ("testcase", "src"):
        return None
    return run_dir.joinpath(*parts)


def is_vacuous(disk_path: Path | None) -> bool:
    """A file is vacuous if it is missing or zero bytes."""
    if disk_path is None:
        return True
    try:
        return not disk_path.is_file() or disk_path.stat().st_size == 0
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def parse_uuid(value: str | UUID | None) -> UUID | None:
    """Best-effort parse of a UUID from a string (or pass-through UUID)."""
    if isinstance(value, UUID):
        return value
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, AttributeError):
        return None


def cache_rate(read_tokens: int, prompt_tokens: int) -> float | None:
    """Cache-hit rate = cache_read / prompt_tokens; ``None`` when undefined."""
    if prompt_tokens <= 0:
        return None
    return read_tokens / prompt_tokens


ROLE_KEYS: tuple[str, ...] = (
    AgentRole.BOSS.value.upper(),
    AgentRole.MANAGER.value.upper(),
    AgentRole.WORKER.value.upper(),
    AgentRole.PENDING.value.upper(),
)
