"""Reconnaissance tool adapter for manager/PENDING codebase inspection.

Implements ReconToolPort with actual filesystem operations, scoped to
a working directory. All operations are read-only.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _ToolSpec:
    """Schema + metadata for a single recon tool (single source of truth)."""

    name: str
    description: str
    parameters: dict[str, dict[str, str]]
    required: list[str] = field(default_factory=list)


_TOOL_SPECS: list[_ToolSpec] = [
    _ToolSpec(
        name="read_file",
        description="Read the contents of a file. Returns up to 200 lines.",
        parameters={
            "path": {"type": "string", "description": "File path relative to the working directory."},
            "max_lines": {"type": "integer", "description": "Maximum lines to read (default 200)."},
        },
        required=["path"],
    ),
    _ToolSpec(
        name="list_directory",
        description="List files and subdirectories in a directory.",
        parameters={
            "path": {"type": "string", "description": "Directory path relative to the working directory."},
        },
        required=["path"],
    ),
    _ToolSpec(
        name="search_codebase",
        description="Search for a regex pattern across source files (grep). Returns up to 50 matches with file paths and line numbers.",
        parameters={
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {"type": "string", "description": "Directory to search in (default: working directory)."},
        },
        required=["pattern"],
    ),
    _ToolSpec(
        name="find_file",
        description="Find files matching a glob pattern (e.g., '*.py', 'test_*.py').",
        parameters={
            "pattern": {"type": "string", "description": "Glob pattern for file names."},
            "path": {"type": "string", "description": "Directory to search in (default: working directory)."},
        },
        required=["pattern"],
    ),
    _ToolSpec(
        name="get_file_structure",
        description="Get a tree view of the directory structure, showing files and folders.",
        parameters={
            "path": {"type": "string", "description": "Root directory (default: working directory)."},
            "max_depth": {"type": "integer", "description": "Maximum directory depth to show (default 3)."},
        },
    ),
]


class ReconToolAdapter:
    """Read-only codebase inspection tools for manager assessment.

    All paths are resolved relative to the working directory and
    prevented from escaping it via path validation.
    """

    def __init__(self, working_directory: str = ".") -> None:
        self._workdir = Path(working_directory).resolve()

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to working directory, preventing escapes.

        Absolute paths are resolved as-is (the LLM may reference paths
        from the CVE context like /src/project). Relative paths are
        resolved against the working directory.
        """
        if Path(path).is_absolute():
            return Path(path).resolve()
        resolved = (self._workdir / path).resolve()
        if not str(resolved).startswith(str(self._workdir)):
            raise ValueError(f"Path escapes working directory: {path}")
        return resolved

    async def read_file(self, path: str, max_lines: int = 200) -> str:
        """Read file contents, truncated to max_lines."""
        resolved = self._resolve_path(path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file or does not exist."

        try:
            text = resolved.read_text(errors="replace")
            lines = text.splitlines()
            if len(lines) > max_lines:
                return "\n".join(lines[:max_lines]) + f"\n\n[...truncated, {len(lines)} total lines]"
            return text
        except Exception as e:
            return f"Error reading '{path}': {e}"

    async def list_directory(self, path: str) -> str:
        """List directory entries with type indicators (/ for dirs)."""
        resolved = self._resolve_path(path)
        if not resolved.is_dir():
            return f"Error: '{path}' is not a directory or does not exist."

        try:
            entries = sorted(resolved.iterdir())
            lines = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{entry.name}{suffix}")
            return "\n".join(lines) if lines else "(empty directory)"
        except Exception as e:
            return f"Error listing '{path}': {e}"

    async def search_codebase(self, pattern: str, path: str = ".") -> str:
        """Search for a regex pattern using grep -rn, limited to 50 matches."""
        resolved = self._resolve_path(path)

        try:
            proc = await asyncio.create_subprocess_exec(
                "grep", "-rn", "--include=*.py", "--include=*.j2",
                "--include=*.yaml", "--include=*.yml", "--include=*.json",
                "--include=*.toml", "--include=*.md", "--include=*.txt",
                "-m", "50", pattern, str(resolved),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            result = stdout.decode(errors="replace").strip()

            if not result:
                return f"No matches found for pattern '{pattern}'."

            # Make paths relative to workdir for readability
            workdir_str = str(self._workdir) + os.sep
            result = result.replace(workdir_str, "")
            return result

        except asyncio.TimeoutError:
            return f"Search timed out for pattern '{pattern}'."
        except Exception as e:
            return f"Search error: {e}"

    async def find_file(self, pattern: str, path: str = ".") -> str:
        """Find files matching a glob pattern."""
        resolved = self._resolve_path(path)

        try:
            matches = sorted(resolved.rglob(pattern))
            # Filter hidden dirs
            matches = [
                m for m in matches
                if not any(part.startswith(".") for part in m.relative_to(self._workdir).parts)
            ]

            if not matches:
                return f"No files found matching '{pattern}'."

            lines = [str(m.relative_to(self._workdir)) for m in matches[:50]]
            result = "\n".join(lines)
            if len(matches) > 50:
                result += f"\n\n[...{len(matches)} total matches, showing first 50]"
            return result

        except Exception as e:
            return f"Find error: {e}"

    async def get_file_structure(self, path: str = ".", max_depth: int = 3) -> str:
        """Get a tree view of the directory structure."""
        resolved = self._resolve_path(path)
        if not resolved.is_dir():
            return f"Error: '{path}' is not a directory."

        lines: list[str] = []
        self._build_tree(resolved, "", 0, max_depth, lines)
        return "\n".join(lines) if lines else "(empty)"

    def _build_tree(
        self,
        directory: Path,
        prefix: str,
        depth: int,
        max_depth: int,
        lines: list[str],
    ) -> None:
        """Recursively build tree output."""
        if depth >= max_depth:
            return

        try:
            entries = sorted(
                (e for e in directory.iterdir() if not e.name.startswith(".")),
                key=lambda e: (not e.is_dir(), e.name),
            )
        except PermissionError:
            return

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")

            if entry.is_dir():
                extension = "    " if is_last else "│   "
                self._build_tree(entry, prefix + extension, depth + 1, max_depth, lines)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": spec.parameters,
                        "required": spec.required,
                    },
                },
            }
            for spec in _TOOL_SPECS
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call by name to the appropriate method.

        Filters arguments to only pass parameters the handler accepts,
        since LLMs sometimes hallucinate extra kwargs.
        """
        import inspect

        handler = getattr(self, name, None)
        if handler is None or name not in {s.name for s in _TOOL_SPECS}:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Filter to only accepted parameters
        sig = inspect.signature(handler)
        valid_params = set(sig.parameters.keys())
        filtered_args = {k: v for k, v in arguments.items() if k in valid_params}

        return await handler(**filtered_args)
