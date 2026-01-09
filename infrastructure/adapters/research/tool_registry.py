"""Research tool registry using OpenHands executors.

This module provides a unified interface for research tools, wrapping
OpenHands executors (grep, glob, file_editor) with OpenAI-format definitions.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openhands.tools.file_editor.definition import FileEditorAction
from openhands.tools.file_editor.impl import FileEditorExecutor
from openhands.tools.glob.definition import GlobAction
from openhands.tools.glob.impl import GlobExecutor
from openhands.tools.grep.definition import GrepAction
from openhands.tools.grep.impl import GrepExecutor

from core.ports.research_port import RESEARCH_TOOL_NAMES
from infrastructure.adapters.research.tools.web_fetch import WebFetchExecutor


@dataclass(frozen=True)
class ToolDefinition:
    """OpenAI-format tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]


# OpenAI-format tool definitions
FILE_READ_TOOL = ToolDefinition(
    name="file_read",
    description="Read the contents of a file. Use this to examine source code, configuration files, or documentation.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute file path to read (e.g., '/workspace/src/main.py')",
            }
        },
        "required": ["path"],
    },
)

GREP_SEARCH_TOOL = ToolDefinition(
    name="grep_search",
    description="Search for a pattern in files. Returns matching file paths. Use regex patterns.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for (e.g., 'class Image', 'def process')",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: working directory)",
            },
            "include": {
                "type": "string",
                "description": "File pattern filter (e.g., '*.py', '*.{ts,tsx}')",
            },
        },
        "required": ["pattern"],
    },
)

LIST_FILES_TOOL = ToolDefinition(
    name="list_files",
    description="List files matching a glob pattern. Use to explore project structure.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (e.g., '**/*.py', 'src/*.ts')",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: working directory)",
            },
        },
        "required": ["pattern"],
    },
)

WEB_FETCH_TOOL = ToolDefinition(
    name="web_fetch",
    description="Fetch content from a URL. Use for external documentation, issue trackers, or reference materials.",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to fetch (must be http:// or https://)",
            },
        },
        "required": ["url"],
    },
)

# Default tools available for research
DEFAULT_TOOLS: list[ToolDefinition] = [
    FILE_READ_TOOL,
    GREP_SEARCH_TOOL,
    LIST_FILES_TOOL,
    WEB_FETCH_TOOL,
]

# Validate all tools match the port contract
_TOOL_NAMES = {t.name for t in DEFAULT_TOOLS}
assert _TOOL_NAMES == RESEARCH_TOOL_NAMES, (
    f"Tool registry mismatch with port: {_TOOL_NAMES} != {RESEARCH_TOOL_NAMES}"
)


class ResearchToolRegistry:
    """Registry for research tools with OpenHands executor backends."""

    def __init__(self, working_directory: str) -> None:
        """Initialize registry with working directory.

        Args:
            working_directory: Base directory for file operations
        """
        self._working_dir = Path(working_directory).resolve()

        # Initialize OpenHands executors
        self._file_editor = FileEditorExecutor(workspace_root=str(self._working_dir))
        self._grep = GrepExecutor(working_dir=str(self._working_dir))
        self._glob = GlobExecutor(working_dir=str(self._working_dir))
        self._web_fetch = WebFetchExecutor()

    def get_tools(
        self, tool_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Get OpenAI-format tool definitions.

        Args:
            tool_names: Specific tools to include (default: all)

        Returns:
            List of OpenAI-format tool definitions
        """
        tools = DEFAULT_TOOLS if tool_names is None else [
            t for t in DEFAULT_TOOLS if t.name in tool_names
        ]

        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by name.

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments

        Returns:
            Tool execution result as string
        """
        if tool_name == "file_read":
            return self._execute_file_read(arguments)
        elif tool_name == "grep_search":
            return self._execute_grep_search(arguments)
        elif tool_name == "list_files":
            return self._execute_list_files(arguments)
        elif tool_name == "web_fetch":
            return self._execute_web_fetch(arguments)
        else:
            return f"Unknown tool: {tool_name}"

    def _execute_file_read(self, arguments: dict[str, Any]) -> str:
        """Execute file read using OpenHands FileEditorExecutor."""
        path = arguments.get("path", "")
        if not path:
            return "Error: path is required"

        action = FileEditorAction(command="view", path=path)
        observation = self._file_editor(action)

        if observation.is_error:
            return f"Error: {observation.text}"
        return observation.text

    def _execute_grep_search(self, arguments: dict[str, Any]) -> str:
        """Execute grep search using OpenHands GrepExecutor."""
        pattern = arguments.get("pattern", "")
        if not pattern:
            return "Error: pattern is required"

        action = GrepAction(
            pattern=pattern,
            path=arguments.get("path"),
            include=arguments.get("include"),
        )
        observation = self._grep(action)

        if observation.is_error:
            return f"Error: {observation.text}"
        return observation.text

    def _execute_list_files(self, arguments: dict[str, Any]) -> str:
        """Execute glob using OpenHands GlobExecutor."""
        pattern = arguments.get("pattern", "*")

        action = GlobAction(
            pattern=pattern,
            path=arguments.get("path"),
        )
        observation = self._glob(action)

        if observation.is_error:
            return f"Error: {observation.text}"
        return observation.text

    def _execute_web_fetch(self, arguments: dict[str, Any]) -> str:
        """Execute web fetch using custom WebFetchExecutor."""
        url = arguments.get("url", "")
        if not url:
            return "Error: url is required"

        return self._web_fetch(url)
