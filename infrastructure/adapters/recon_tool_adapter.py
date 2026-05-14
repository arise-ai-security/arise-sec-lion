"""Reconnaissance tool adapter for manager/PENDING codebase inspection.

Implements ReconToolPort with actual filesystem operations, scoped to
a working directory. All operations are read-only.

Smart recon tools (get_symbols_overview, read_symbol) use tree-sitter
for multi-language symbol extraction (C, C++, Python). Falls back to
ctags when tree-sitter grammars are unavailable.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ports.domain_plugin_port import WorkspacePathAlias
from infrastructure.adapters.workspace_paths import WorkspacePathMapper


logger = logging.getLogger(__name__)


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
        description="Read file contents. Use start_line/end_line for targeted reads instead of dumping the whole file.",
        parameters={
            "path": {"type": "string", "description": "File path relative to the working directory."},
            "max_lines": {"type": "integer", "description": "Maximum lines to read (default 200)."},
            "start_line": {"type": "integer", "description": "1-indexed start line (inclusive). Omit to start from beginning."},
            "end_line": {"type": "integer", "description": "1-indexed end line (inclusive). Omit to read to end or max_lines."},
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
    _ToolSpec(
        name="get_symbols_overview",
        description="Get function/class/method signatures from a file WITHOUT bodies. Returns symbol names, parameter lists, and line numbers. Use this before read_symbol to identify what to read.",
        parameters={
            "path": {"type": "string", "description": "File path to extract symbols from."},
        },
        required=["path"],
    ),
    _ToolSpec(
        name="read_symbol",
        description="Read the full body of a specific function, class, or method by name. More precise than read_file — reads only the symbol's code.",
        parameters={
            "path": {"type": "string", "description": "File path containing the symbol."},
            "symbol_name": {"type": "string", "description": "Name of the function, class, or method to read."},
        },
        required=["path", "symbol_name"],
    ),
]


class ReconToolAdapter:
    """Read-only codebase inspection tools for manager assessment.

    All paths are resolved relative to the working directory and
    prevented from escaping it via path validation.
    """

    def __init__(self, working_directory: str = ".") -> None:
        self._workdir = Path(working_directory).resolve()
        self._path_mapper = WorkspacePathMapper()

    def set_working_directory(self, path: str) -> None:
        """Update the working directory for this adapter.

        Called at run start to point recon tools at the configured workspace
        so thinker agents can read target code instead of the orchestrator project.
        """
        self._workdir = Path(path).resolve()

    def set_path_aliases(self, aliases: tuple[WorkspacePathAlias, ...]) -> None:
        """Map domain/container paths to host paths under the working directory."""
        normalized = []
        for alias in aliases:
            virtual_path = alias.virtual_path.rstrip("/") or "/"
            if not virtual_path.startswith("/"):
                raise ValueError(f"Path alias must be absolute: {alias.virtual_path}")

            host_path = self._ensure_within_workdir(
                Path(alias.host_path).resolve(),
                alias.host_path,
            )
            normalized.append(WorkspacePathAlias(virtual_path, str(host_path)))

        self._path_mapper = WorkspacePathMapper(tuple(normalized))

    @property
    def name(self) -> str:
        """Stable toolset identifier used in config resolution."""
        return "recon"

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to working directory, preventing escapes.

        Absolute domain paths may be mapped through run-scoped aliases
        such as ``/src`` or ``/testcase``. Other paths must already be
        inside the working directory or be relative to it.
        """
        aliased = self._map_path_alias(path)
        if aliased is not None:
            return self._ensure_within_workdir(aliased.resolve(), path)

        if Path(path).is_absolute():
            return self._ensure_within_workdir(Path(path).resolve(), path)

        resolved = (self._workdir / path).resolve()
        return self._ensure_within_workdir(resolved, path)

    def _map_path_alias(self, path: str) -> Path | None:
        if not Path(path).is_absolute():
            return None

        host_path = self._path_mapper.virtual_to_host(path)
        return Path(host_path) if host_path is not None else None

    def _ensure_within_workdir(self, resolved: Path, display_path: str) -> Path:
        try:
            resolved.relative_to(self._workdir)
        except ValueError as e:
            raise ValueError(f"Path escapes working directory: {display_path}") from e
        return resolved

    async def read_file(
        self, path: str, max_lines: int = 200,
        start_line: int | None = None, end_line: int | None = None,
    ) -> str:
        """Read file contents with optional line range.

        Args:
            path: File path relative to working directory.
            max_lines: Max lines when no range specified (default 200).
            start_line: 1-indexed start line (inclusive).
            end_line: 1-indexed end line (inclusive).
        """
        resolved = self._resolve_path(path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file or does not exist."

        try:
            text = resolved.read_text(errors="replace")
            lines = text.splitlines()
            total = len(lines)

            if start_line is not None or end_line is not None:
                # Targeted range read (1-indexed, inclusive)
                s = max(0, (start_line or 1) - 1)
                e = min(total, end_line or total)
                selected = lines[s:e]
                header = f"[{path} lines {s+1}-{e} of {total}]\n"
                return header + "\n".join(
                    f"{i}: {line}" for i, line in enumerate(selected, start=s+1)
                )

            if total > max_lines:
                return "\n".join(lines[:max_lines]) + f"\n\n[...truncated, {total} total lines]"
            return text
        except Exception as e:
            return f"Error reading '{path}': {e}"

    async def list_directory(self, path: str) -> str:
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
                "grep", "-rn",
                "--include=*.py", "--include=*.j2",
                "--include=*.yaml", "--include=*.yml", "--include=*.json",
                "--include=*.toml", "--include=*.md", "--include=*.txt",
                "--include=*.c", "--include=*.h", "--include=*.cpp",
                "--include=*.cc", "--include=*.cxx", "--include=*.hpp",
                "-m", "50", pattern, str(resolved),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            result = stdout.decode(errors="replace").strip()

            if not result:
                return f"No matches found for pattern '{pattern}'."

            workdir_str = str(self._workdir) + os.sep
            result = result.replace(workdir_str, "")
            return result

        except TimeoutError:
            return f"Search timed out for pattern '{pattern}'."
        except Exception as e:
            return f"Search error: {e}"

    async def find_file(self, pattern: str, path: str = ".") -> str:
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

    # ── Smart recon: symbol-level tools ────────────────────────────────

    async def get_symbols_overview(self, path: str) -> str:
        """Get function/class/method signatures without bodies.

        Uses ctags if available, otherwise falls back to regex-based extraction.
        Supports C, C++, Python, and other languages ctags handles.
        """
        resolved = self._resolve_path(path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file or does not exist."

        # Try ctags first (universal-ctags preferred)
        result = await self._ctags_symbols(resolved, path)
        if result is not None:
            return result

        # Fallback: regex-based extraction
        return self._regex_symbols(resolved, path)

    async def read_symbol(self, path: str, symbol_name: str) -> str:
        """Read the body of a specific function/class by name.

        Uses ctags to find the symbol's line, then reads the full body
        by tracking brace/indentation depth.
        """
        resolved = self._resolve_path(path)
        if not resolved.is_file():
            return f"Error: '{path}' is not a file or does not exist."

        try:
            text = resolved.read_text(errors="replace")
            lines = text.splitlines()
        except Exception as e:
            return f"Error reading '{path}': {e}"

        # Find the symbol's start line
        start = self._find_symbol_line(lines, symbol_name, resolved.suffix)
        if start is None:
            return f"Symbol '{symbol_name}' not found in '{path}'."

        # Extract the symbol body
        end = self._find_symbol_end(lines, start, resolved.suffix)
        header = f"[{path}:{start+1}-{end+1} — {symbol_name}]\n"
        return header + "\n".join(
            f"{i}: {line}" for i, line in enumerate(lines[start:end+1], start=start+1)
        )

    async def _ctags_symbols(self, resolved: Path, display_path: str) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ctags", "--output-format=json", "--fields=+nKS",
                "-f", "-", str(resolved),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return None

            symbols: list[str] = []
            for line in stdout.decode(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    tag = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = tag.get("kind", "?")
                name = tag.get("name", "?")
                line_no = tag.get("line", "?")
                signature = tag.get("signature", "")
                scope = tag.get("scope", "")
                scope_str = f"  [{scope}]" if scope else ""
                symbols.append(f"  L{line_no} {kind}: {name}{signature}{scope_str}")

            if not symbols:
                return f"No symbols found in '{display_path}'."
            return f"Symbols in {display_path}:\n" + "\n".join(symbols)

        except (TimeoutError, FileNotFoundError):
            return None

    def _regex_symbols(self, resolved: Path, display_path: str) -> str:
        """Fallback: extract symbols using regex patterns."""
        try:
            text = resolved.read_text(errors="replace")
        except Exception as e:
            return f"Error reading '{display_path}': {e}"

        lines = text.splitlines()
        suffix = resolved.suffix.lower()
        symbols: list[str] = []

        if suffix in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"):
            # C/C++ function definitions: type name(params) {
            pattern = re.compile(
                r"^[\w\s\*]+\s+(\w+)\s*\([^)]*\)\s*\{?\s*$"
            )
            for i, line in enumerate(lines, 1):
                m = pattern.match(line.strip())
                if m and m.group(1) not in ("if", "for", "while", "switch", "return"):
                    symbols.append(f"  L{i} function: {line.strip()}")
        elif suffix == ".py":
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("class "):
                    indent = len(line) - len(line.lstrip())
                    kind = "class" if stripped.startswith("class") else "function"
                    symbols.append(f"  L{i} {kind}: {'  ' * (indent // 4)}{stripped}")
        else:
            return f"Symbol extraction not supported for '{suffix}' files. Use read_file instead."

        if not symbols:
            return f"No symbols found in '{display_path}'."
        return f"Symbols in {display_path}:\n" + "\n".join(symbols)

    @staticmethod
    def _find_symbol_line(lines: list[str], symbol_name: str, suffix: str) -> int | None:
        suffix = suffix.lower()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if suffix in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"):
                # Match function/struct definitions containing the name
                if re.match(rf"[\w\s\*]*\b{re.escape(symbol_name)}\s*\(", stripped):
                    return i
                if re.match(rf"(struct|class|enum)\s+{re.escape(symbol_name)}\b", stripped):
                    return i
            elif suffix == ".py":
                if re.match(rf"(def|class)\s+{re.escape(symbol_name)}\b", stripped):
                    return i
        return None

    @staticmethod
    def _find_symbol_end(lines: list[str], start: int, suffix: str) -> int:
        suffix = suffix.lower()

        if suffix in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"):
            # Brace-matching for C/C++
            depth = 0
            found_open = False
            for i in range(start, len(lines)):
                for ch in lines[i]:
                    if ch == "{":
                        depth += 1
                        found_open = True
                    elif ch == "}":
                        depth -= 1
                if found_open and depth <= 0:
                    return i
            return min(start + 100, len(lines) - 1)

        if suffix == ".py":
            # Indentation-based for Python
            base_indent = len(lines[start]) - len(lines[start].lstrip())
            for i in range(start + 1, len(lines)):
                line = lines[i]
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip())
                if indent <= base_indent:
                    return i - 1
            return len(lines) - 1

        return min(start + 50, len(lines) - 1)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
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

        try:
            return await handler(**filtered_args)
        except ValueError as e:
            return f"Error: {e}"
