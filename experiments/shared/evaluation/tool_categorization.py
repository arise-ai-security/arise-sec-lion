"""Reconstruct and categorize worker tool calls from the event stream.

Maps each ``ThoughtCaptured`` tool-use event to a :class:`ToolCall` with a
:class:`ToolCategory`, parsing OpenHands action payloads (file-editor commands,
MCP shell calls, security-tool invocations) opaquely from their content. Pure
over an in-memory event list; no database access.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from core.domain.events.events import ThoughtCaptured
from experiments.shared.evaluation.models import ToolCall, ToolCategory


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from core.domain.events.events import DomainEvent


logger = logging.getLogger(__name__)

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
    # Local import breaks the common <-> tool_categorization re-export cycle
    # (ruff PLC0415 is repo-ignored for exactly this).
    from experiments.shared.evaluation.common import events_of_type

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
