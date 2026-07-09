"""Extract file touches from the two access channels (correctness req #2/#3).

Channel 1 — SDK tool calls (`ThoughtCaptured`, ``output_type == "tool_use"``):
``content`` is a human prefix + ``\\nInput: {json}``. Read/Write/Edit carry a
clean ``file_path`` (or the ``Reading:/Writing:/Editing:`` prefix); Bash/security
shell carry a ``command`` whose paths are parsed conservatively (only the known
container zones) at LOW confidence.

Channel 2 — recon probes (`ProbeCompleted`): the requested path is NOT a field;
it is scraped from ``result_summary`` per ``probe_type`` (``read_symbol`` /
``read_file`` headers, ``search_codebase`` match lines). Directory-structure
probes expose no path (confidence ``none``).

Every touch carries a ``confidence`` so the metrics layer can exclude
low-confidence paths from headline overlap statistics, and a ``zone``
(``src`` / ``testcase`` / ``work`` / ``other``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.text.prefixes import recover_tool_name


if TYPE_CHECKING:
    from datetime import datetime

    from experiments.shared.scripts.db.models import EventRow


_INPUT_SEP = "\nInput: "
_PREFIX_PATH_RE = re.compile(r"^(?:Reading|Writing|Editing|Creating): (.+)$")
# recon result_summary headers: "[/abs/path:12-30 — sym]" and "[/abs/path lines 1-80 of N]"
_SYMBOL_HDR_RE = re.compile(r"^\[(/[^\s:\]]+):")
_FILE_HDR_RE = re.compile(r"^\[(/\S+?)\s+(?:lines?|of)\b")
# search_codebase match line: "src/foo/bar.c:42:  matched text"
_SEARCH_LINE_RE = re.compile(r"^([^\s:]+\.[A-Za-z0-9_+.-]+):\d+:")
# container-rooted paths inside a shell command (conservative: known zones only)
_CMD_PATH_RE = re.compile(r"(?<![\w/])(/(?:src|testcase|work)/[^\s;:|&'\"<>()]+)")

_READ_TOOLS = {"Read", "read_file"}
_WRITE_TOOLS = {"Write", "write_file"}
_EDIT_TOOLS = {"Edit", "MultiEdit", "str_replace_editor"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_SHELL_TOOLS = {"Bash", "mcp__security_tools__shell_in_container", "execute_command", "shell_execute"}

_RECON_READ_PROBES = {"read_file", "read_symbol"}
_RECON_STRUCTURE_PROBES = {"get_file_structure", "get_symbols_overview"}


@dataclass(frozen=True)
class Touch:
    """One file access by one node, attributed to a channel/op with confidence."""

    node_id: str
    channel: str  # "sdk" | "recon"
    tool: str  # tool_name or probe_type
    op: str  # read | write | edit | search | exec | structure
    path: str | None  # normalized container path, or None
    confidence: str  # high | med | low | none
    zone: str | None  # src | testcase | work | other | None
    occurred_at: datetime | str
    seq: int


def normalize_path(path: str | None) -> str | None:
    """Canonicalize a path so the two channels agree on the same file.

    Recon ``read_*`` headers are absolute (``/src/x``) while ``search_codebase``
    matches are repo-relative (``src/x``); the container root is ``/``, so a
    relative path with a directory component gets a leading slash.
    """
    if not path:
        return None
    path = path.strip().strip("`\"'")
    if not path:
        return None
    if not path.startswith("/") and "/" in path:
        path = "/" + path
    return path


def zone_of(path: str | None) -> str | None:
    if not path:
        return None
    head = path.lstrip("/").split("/", 1)[0]
    return head if head in ("src", "testcase", "work") else "other"


def _split_content(content: str) -> tuple[str, dict]:
    """Return (first prefix line, parsed Input JSON dict)."""
    idx = content.find(_INPUT_SEP)
    if idx == -1:
        return content.split("\n", 1)[0], {}
    first = content[:idx].split("\n", 1)[0].strip()
    try:
        parsed = json.loads(content[idx + len(_INPUT_SEP):])
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    return first, parsed if isinstance(parsed, dict) else {}


def _path_from_prefix(first_line: str) -> str | None:
    match = _PREFIX_PATH_RE.match(first_line)
    return match.group(1).strip() if match else None


def extract_command_paths(command: str) -> list[str]:
    """Conservative: only container-zoned paths (``/src|/testcase|/work``)."""
    seen: dict[str, None] = {}  # ordered de-dup
    for match in _CMD_PATH_RE.finditer(command or ""):
        seen.setdefault(match.group(1).rstrip(".,"), None)
    return list(seen)


def _touch(event: EventRow, channel: str, tool: str, op: str, path: str | None,
           confidence: str) -> Touch:
    norm = normalize_path(path)
    return Touch(
        node_id=str(event.aggregate_id), channel=channel, tool=tool, op=op,
        path=norm, confidence=confidence if norm else "none", zone=zone_of(norm),
        occurred_at=event.occurred_at, seq=event.sequence_number,
    )


def extract_sdk_touches(event: EventRow) -> list[Touch]:
    """Touches from one ``ThoughtCaptured`` tool_use event."""
    content = event.payload.get("content") or ""
    tool = recover_tool_name(content, event.payload.get("tool_name"))
    first, inp = _split_content(content)

    if tool in _READ_TOOLS:
        path = inp.get("file_path") or inp.get("path") or _path_from_prefix(first)
        return [_touch(event, "sdk", tool, "read", path, "high")]
    if tool in _WRITE_TOOLS:
        path = inp.get("file_path") or inp.get("path") or _path_from_prefix(first)
        return [_touch(event, "sdk", tool, "write", path, "high")]
    if tool in _EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("path") or _path_from_prefix(first)
        return [_touch(event, "sdk", tool, "edit", path, "high")]
    if tool in _SEARCH_TOOLS:
        return [_touch(event, "sdk", tool, "search", inp.get("path"), "med")]
    if tool in _SHELL_TOOLS:
        paths = extract_command_paths(inp.get("command") or inp.get("cmd") or "")
        if not paths:
            return [_touch(event, "sdk", tool, "exec", None, "none")]
        return [_touch(event, "sdk", tool, "exec", p, "low") for p in paths]
    if tool == "mcp__security_tools__valgrind_run":
        path = inp.get("binary") or inp.get("target") or inp.get("path")
        return [_touch(event, "sdk", tool, "exec", path, "low")]
    return [_touch(event, "sdk", tool, "exec", None, "none")]


def extract_probe_touches(event: EventRow) -> list[Touch]:
    """Touches from one ``ProbeCompleted`` recon event."""
    probe = event.payload.get("probe_type") or ""
    summary = event.payload.get("result_summary") or ""

    if probe == "read_symbol":
        match = _SYMBOL_HDR_RE.match(summary)
        return [_touch(event, "recon", probe, "read", match.group(1) if match else None, "high")]
    if probe == "read_file":
        match = _FILE_HDR_RE.match(summary)
        return [_touch(event, "recon", probe, "read", match.group(1) if match else None, "high")]
    if probe == "search_codebase":
        paths = []
        seen: set[str] = set()
        for line in summary.splitlines():
            match = _SEARCH_LINE_RE.match(line)
            if match and match.group(1) not in seen:
                seen.add(match.group(1))
                paths.append(match.group(1))
        if not paths:
            return [_touch(event, "recon", probe, "search", None, "none")]
        return [_touch(event, "recon", probe, "search", p, "med") for p in paths]
    if probe in _RECON_STRUCTURE_PROBES:
        return [_touch(event, "recon", probe, "structure", None, "none")]
    return [_touch(event, "recon", probe, "read", None, "none")]


def extract_touches(event: EventRow) -> list[Touch]:
    """All file touches from one event (empty for non-access events)."""
    if event.event_type == "ThoughtCaptured" and event.payload.get("output_type") == "tool_use":
        return extract_sdk_touches(event)
    if event.event_type == "ProbeCompleted":
        return extract_probe_touches(event)
    return []
