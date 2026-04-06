"""Container session helpers for worker adapters."""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ContainerSessionContext:
    """Serializable SEC-bench container session for worker adapters."""

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
        )

    def apply_task_prefix(self, task_description: str, *, auto_shell: bool) -> str:
        """Explain the host/container mapping to the worker."""
        work_dir = self.relative_host_work_dir()
        if auto_shell:
            shell_line = (
                "- Shell commands are already routed into the SEC-bench "
                "container by this runtime.\n"
                '- Use normal shell commands, or `./secb-exec "<command>"` as a fallback.\n'
            )
        else:
            shell_line = (
                '- **ALL build/test/runtime shell commands** MUST use '
                '`./secb-exec "<command>"`.  '
                "Do NOT run them directly — they will fail because "
                "container paths do not exist on this host.\n"
                '- Example: `./secb-exec "cd /src/imagemagick && make"` '
                "(NOT `cd /src/imagemagick && make`)\n"
            )
        prefix = (
            "## Container-backed SEC-bench workspace\n\n"
            "⚠️ **ABSOLUTE RULE — TWO SEPARATE FILESYSTEMS:**\n"
            "You are running on a HOST machine. Source code and testcase files are "
            "mirrored between host and a Docker container. The file editor tool "
            "operates on HOST paths. Container paths like `/src/...` and "
            "`/testcase/...` DO NOT EXIST on the host and WILL ERROR.\n\n"
            "**File editor (read/write/edit) — ALWAYS use ABSOLUTE host paths:**\n"
            f"- Source code: `{self.host_source_dir}/...` — "
            "NOT `/src/...`\n"
            f"- Test artifacts: `{self.host_testcase_dir}/...` — "
            "NOT `/testcase/...`\n"
            f"- Working dir: `{self.host_work_dir}/...` — "
            f"NOT `{self.container_working_directory}/...`\n"
            f"- Example: to read rla.c → `{self.host_source_dir}/imagemagick/coders/rla.c`\n"
            f"- Example: to read repro.sh → `{self.host_testcase_dir}/repro.sh`\n\n"
            "**Shell commands — ALWAYS use secb-exec for container commands:**\n"
            f"{shell_line}\n"
            f"**Source code is already cloned** at `{self.host_source_dir}/` with the "
            "correct commit checked out. Do NOT re-clone the repository.\n\n"
            f'**Helper**: `./{self.helper_script.name} "<command>"` '
            "runs a shell command inside the container.\n\n"
        )
        return f"{prefix}\n{task_description}"

    def translate_tool_input(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Translate container paths and shell commands for Claude Code tools."""
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
        """Translate well-known container paths onto the host mirror."""
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
        if stripped.startswith(("./secb-exec ", "secb-exec ")):
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
        """Return the mirrored working directory relative to the workspace root."""
        try:
            rel = self.host_work_dir.relative_to(self.workspace_root)
            rel_str = rel.as_posix()
            return f"./{rel_str}" if rel_str != "." else "."
        except ValueError:
            return str(self.host_work_dir)
