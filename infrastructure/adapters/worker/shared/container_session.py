"""Container session helpers for worker adapters."""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ContainerSessionContext:
    """Serializable container session context shared by worker adapters."""

    container_id: str
    container_name: str
    image: str
    workspace_root: Path
    host_source_dir: Path
    host_testcase_dir: Path
    host_work_dir: Path
    container_source_dir: str
    container_testcase_dir: str
    container_working_directory: str
    helper_script: Path
    container_work_dir: str = "/work"
    container_workspace_root: str = "/arise-run"

    @classmethod
    def from_task_context(
        cls, task_context: dict[str, Any]
    ) -> "ContainerSessionContext | None":
        raw = task_context.get("container_session")
        if not isinstance(raw, dict):
            return None
        return cls(
            container_id=str(raw["container_id"]),
            container_name=str(raw["container_name"]),
            image=str(raw["image"]),
            workspace_root=Path(raw["workspace_root"]),
            host_source_dir=Path(raw["host_source_dir"]),
            host_testcase_dir=Path(raw["host_testcase_dir"]),
            host_work_dir=Path(raw["host_work_dir"]),
            container_source_dir=str(raw["container_source_dir"]),
            container_testcase_dir=str(raw["container_testcase_dir"]),
            container_working_directory=str(raw["container_working_directory"]),
            helper_script=Path(raw["helper_script"]),
            container_work_dir=str(raw.get("container_work_dir", "/work")),
            container_workspace_root=str(raw.get("container_workspace_root", "/arise-run")),
        )

    @property
    def host_work_root(self) -> Path:
        return self.workspace_root / "work"

    def host_to_container_path(self, host_path: Path) -> str:
        """Translate a host path under ``workspace_root`` to its in-container path.

        Used by the Cell A docker-exec runner to point the in-container CLI at
        scratch files (mcp config, ``CLAUDE_CONFIG_DIR``) the host wrote under
        ``workspace_root`` (which is bind-mounted at ``container_workspace_root``).
        """
        try:
            rel = host_path.resolve().relative_to(self.workspace_root.resolve())
        except ValueError as e:
            raise ValueError(
                f"{host_path} is not under workspace_root {self.workspace_root}; "
                "cannot translate to container path"
            ) from e
        rel_str = rel.as_posix()
        if rel_str in {"", "."}:
            return self.container_workspace_root
        return f"{self.container_workspace_root}/{rel_str}"

    def apply_task_prefix(self, task_description: str, *, auto_shell: bool) -> str:
        """Explain the host/container mapping to the worker.

        Build/test/runtime commands must run inside the target container,
        not on the host. Workers reach the container through the MCP
        ``shell_in_container`` tool (see
        ``plugins/security/mcp/security_tools_server.py``). When ``auto_shell``
        is true the adapter's permission hook ALSO rewrites a Bash tool call
        into a ``docker exec`` against the container, but the agent should
        prefer ``shell_in_container`` so the MCP server is the single source
        of truth for container locality.
        """
        if auto_shell:
            shell_line = (
                "- The MCP `shell_in_container` tool is the recommended way "
                "to run build/test/runtime commands.\n"
                "- Plain Bash tool calls are auto-routed into the container "
                "by this runtime as a fallback, but `shell_in_container` is "
                "preferred for clarity.\n"
                "- Do not place host mirror paths in shell commands; use "
                "`/src`, `/testcase`, and `/work` paths.\n"
            )
        else:
            shell_line = (
                "- **ALL build/test/runtime shell commands** MUST use the MCP "
                "`shell_in_container` tool. Do NOT run them on the host "
                "terminal — host paths like `/src`, `/testcase`, and `/work` do not "
                "exist on the host and the command will fail.\n"
                '- Example: call `shell_in_container` with '
                '`command="cd /src/<project> && make"` (NOT a plain shell '
                "command that runs on the host).\n"
                "- Never call `shell_in_container` with a command containing "
                f"`{self.host_source_dir}` or any other host mirror path.\n"
            )
        prefix = (
            "## Container-backed workspace\n\n"
            "⚠️ **ABSOLUTE RULE — TWO SEPARATE FILESYSTEMS:**\n"
            "You are running on a HOST machine. Source code and testcase files are "
            "mirrored between host and a Docker container. Build artifacts are "
            "mirrored through `/work`. Native file tools operate on the HOST "
            "mirror. The MCP `shell_in_container` tool runs INSIDE the container. "
            "Host paths under the run directory DO NOT EXIST inside the container.\n\n"
            "**Native file tools (read/write/edit/search) — use host paths or "
            "container aliases:**\n"
            f"- Source code: `{self.host_source_dir}/...` — "
            "container alias `/src/...`\n"
            f"- Test artifacts: `{self.host_testcase_dir}/...` — "
            "container alias `/testcase/...`\n"
            f"- Build artifacts: `{self.host_work_root}/...` — "
            "container alias `/work/...`\n"
            f"- Repository root for file tools: `{self.host_work_dir}/...` — "
            f"container alias `{self.container_working_directory}/...`\n"
            f"- Example: to read a source file → "
            f"`{self.host_source_dir}/<project>/src/file.c` "
            "or `/src/<project>/src/file.c`\n"
            f"- Example: to read a testcase artifact → "
            f"`{self.host_testcase_dir}/<testcase>.sh` or "
            "`/testcase/<testcase>.sh`\n\n"
            "**Shell commands — ALWAYS use the MCP `shell_in_container` tool "
            "with CONTAINER paths, never host mirror paths:**\n"
            f"- Source code in shell: `{self.container_source_dir}/...`, not "
            f"`{self.host_source_dir}/...`\n"
            f"- Test artifacts in shell: `{self.container_testcase_dir}/...`, not "
            f"`{self.host_testcase_dir}/...`\n"
            f"- Build artifacts in shell: `{self.container_work_dir}/...`, not "
            f"`{self.host_work_root}/...`\n"
            f"{shell_line}\n"
            f"**Source code is already cloned** at `{self.container_source_dir}/` "
            "inside the container and mirrored on the host. Do NOT re-clone "
            "the repository.\n\n"
            "**Tool**: the MCP `shell_in_container` tool runs an arbitrary "
            "shell command inside the active container — use it whenever you "
            "need a container-only path that does not exist on the host.\n\n"
        )
        return f"{prefix}\n{task_description}"

    def translate_tool_input(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        updated = dict(tool_input)
        for key in ("file_path", "path", "cwd", "working_directory"):
            value = updated.get(key)
            if isinstance(value, str):
                updated[key] = self.map_container_path(value)

        if tool_name == "Bash":
            command = updated.get("command")
            if isinstance(command, str):
                updated["command"] = self.wrap_shell_command(command)
        return updated

    def map_container_path(self, path: str) -> str:
        if path == self.container_source_dir:
            return str(self.host_source_dir)
        if path.startswith(f"{self.container_source_dir}/"):
            suffix = path.removeprefix(self.container_source_dir).lstrip("/")
            return str(self.host_source_dir / suffix)
        if path == self.container_testcase_dir:
            return str(self.host_testcase_dir)
        if path.startswith(f"{self.container_testcase_dir}/"):
            suffix = path.removeprefix(self.container_testcase_dir).lstrip("/")
            return str(self.host_testcase_dir / suffix)
        if path == self.container_work_dir:
            return str(self.host_work_root)
        if path.startswith(f"{self.container_work_dir}/"):
            suffix = path.removeprefix(self.container_work_dir).lstrip("/")
            return str(self.host_work_root / suffix)
        return path

    def wrap_shell_command(self, command: str) -> str:
        """Run a shell command inside the active container.

        Uses ``cd`` instead of ``-w`` so that commands survive if the
        working directory is temporarily deleted (e.g. during re-clone).
        """
        stripped = command.strip()
        if not stripped:
            return command
        if stripped.startswith("docker exec "):
            return command
        helper_name = self.helper_script.name
        if stripped.startswith((f"./{helper_name} ", f"{helper_name} ")):
            return command
        cd_prefix = (
            f"cd {shlex.quote(self.container_working_directory)} 2>/dev/null "
            f"|| cd {shlex.quote(self.container_source_dir)} 2>/dev/null "
            "|| cd /; "
        )
        inner = cd_prefix + command
        return (
            "docker exec -i "
            f"{shlex.quote(self.container_id)} "
            f"bash -lc {shlex.quote(inner)}"
        )

    def relative_host_work_dir(self) -> str:
        try:
            rel = self.host_work_dir.relative_to(self.workspace_root)
            rel_str = rel.as_posix()
            return f"./{rel_str}" if rel_str != "." else "."
        except ValueError:
            return str(self.host_work_dir)
