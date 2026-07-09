"""Tool-name recovery from ``ThoughtCaptured`` content (design doc §7).

The A1/A2 historic data path drops the structured ``tool_name`` field
(see ``claude_code_worker.py:609``); the only signal left is the leading
prefix of the rendered content string emitted by ``format_tool_event``.
This module unifies the recovery so callers do not branch on which path
produced the row.
"""

from __future__ import annotations

import re
from typing import Final


_KNOWN_PREFIX_TO_TOOL: Final[dict[str, str]] = {
    "Running: ": "Bash",
    "Reading: ": "Read",
    "Writing: ": "Write",
    "Editing: ": "Edit",
    "Searching files: ": "Glob",
    "Searching content: ": "Grep",
}
_TOOL_FALLTHROUGH_RE: Final[re.Pattern[str]] = re.compile(r"^Tool: (\S+)")


def recover_tool_name(content: str, tool_name_field: str | None) -> str:
    """Return the tool name for a ``tool_use`` ThoughtCaptured event.

    Precedence:
      1. Structured ``tool_name_field`` if non-empty (B-family / post-bugfix A).
      2. Content prefix table (A1/A2 historic data).
      3. ``Tool: <name>`` fallthrough for tools without a custom prefix.
      4. Fallback ``"unknown"`` — always counted, never crashes.
    """
    if tool_name_field:
        return tool_name_field
    first_line = content.split("\n", 1)[0]
    for prefix, name in _KNOWN_PREFIX_TO_TOOL.items():
        if first_line.startswith(prefix):
            return name
    m = _TOOL_FALLTHROUGH_RE.match(first_line)
    if m:
        return m.group(1)
    return "unknown"
