"""Tool event formatters for worker adapters.

Provides human-readable formatting for tool invocations.
Uses registry pattern for extensibility (OCP).
"""

from collections.abc import Callable
from typing import Any


def _truncate(text: str, max_length: int) -> str:
    """Truncate text with ellipsis if exceeds max_length."""
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}..."


# Tool formatters registry (OCP - extensible without modification)
TOOL_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    # Claude Code tools
    "Bash": lambda i: f"Running: {i.get('description') or _truncate(i.get('command', ''), 100)}",
    "Write": lambda i: f"Writing: {i.get('file_path', '')}",
    "Edit": lambda i: f"Editing: {i.get('file_path', '')}",
    "Read": lambda i: f"Reading: {i.get('file_path', '')}",
    "Glob": lambda i: f"Searching files: {i.get('pattern', '')}",
    "Grep": lambda i: f"Searching content: {i.get('pattern', '')}",
    # MCP filesystem tools (Google ADK)
    "read_file": lambda i: f"Reading: {i.get('path', '')}",
    "write_file": lambda i: f"Writing: {i.get('path', '')}",
    "list_directory": lambda i: f"Listing: {i.get('path', '')}",
    "create_directory": lambda i: f"Creating dir: {i.get('path', '')}",
    # Shell execution (Google ADK)
    "execute_command": lambda i: f"Running: {_truncate(i.get('command', ''), 100)}",
    "shell_execute": lambda i: f"Running: {_truncate(i.get('command', ''), 100)}",
}


def format_tool_event(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format tool invocation as human-readable content.

    Uses registry pattern for extensibility (OCP).

    Args:
        tool_name: Name of the tool being invoked.
        tool_input: Tool input parameters.

    Returns:
        Human-readable description of the tool invocation.
    """
    formatter = TOOL_FORMATTERS.get(tool_name)
    return formatter(tool_input) if formatter else f"Tool: {tool_name}"
