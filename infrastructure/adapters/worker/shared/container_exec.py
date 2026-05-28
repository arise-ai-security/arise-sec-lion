"""Shared helpers for running worker engines inside domain-provided containers."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import stat
from pathlib import Path
from typing import Any

from infrastructure.io.atomic_write import write_text

from .container_session import ContainerSessionContext


logger = logging.getLogger(__name__)


CONTAINER_AGENT_UID = "1000"
CONTAINER_AGENT_GID = "1000"
CONTAINER_AGENT_USER = "arise-agent"
CONTAINER_AGENT_HOME = "/tmp/arise-claude-home"  # noqa: S108
CONTAINER_ROOT_HELPER = "/usr/local/bin/arise-root"
CONTAINER_ROOT_BASH = "/usr/local/bin/arise-root-bash"
CONTAINER_SUDO_SHIM = "/usr/local/bin/sudo"
CONTAINER_APT_GET_SHIM = "/usr/local/bin/apt-get"
CONTAINER_USER_PREP_TIMEOUT_SECONDS = 10
CONTAINER_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "TZ"}
)
PROCESS_KILL_GRACE_SECONDS = 5


def build_claude_container_env(*, scratch_container_dir: str | None) -> dict[str, str]:
    """Build the narrow environment forwarded to in-container Claude."""
    env: dict[str, str] = {}
    for key in sorted(CONTAINER_ENV_ALLOWLIST):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["HOME"] = CONTAINER_AGENT_HOME
    env["USER"] = CONTAINER_AGENT_USER
    env["LOGNAME"] = CONTAINER_AGENT_USER
    env["SHELL"] = "/bin/bash"
    if scratch_container_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = scratch_container_dir
    return env


def build_docker_exec_argv(
    *,
    container_session: ContainerSessionContext,
    command_argv: list[str],
    env: dict[str, str],
    user: str | None = f"{CONTAINER_AGENT_UID}:{CONTAINER_AGENT_GID}",
    workdir: str | None = None,
) -> list[str]:
    """Build ``docker exec`` argv for a command inside the active container."""
    wrapped: list[str] = ["docker", "exec", "-i"]
    if user is not None:
        wrapped.extend(["--user", user])
    wrapped.extend(["-w", workdir or container_session.container_working_directory])
    for key, value in env.items():
        wrapped.extend(["-e", f"{key}={value}"])
    wrapped.append(container_session.container_id)
    wrapped.extend(command_argv)
    return wrapped


def write_docker_exec_wrapper(
    *,
    path: Path,
    container_session: ContainerSessionContext,
    executable: str,
    env: dict[str, str],
    user: str | None = f"{CONTAINER_AGENT_UID}:{CONTAINER_AGENT_GID}",
    workdir: str | None = None,
) -> Path:
    """Write an executable host wrapper that docker-execs into the container."""
    argv = build_docker_exec_argv(
        container_session=container_session,
        command_argv=[executable],
        env=env,
        user=user,
        workdir=workdir,
    )
    command = " ".join(shlex.quote(part) for part in argv)
    write_text(
        path,
        f'#!/bin/sh\nset -eu\nexec {command} "$@"\n',
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


async def prepare_claude_container_user(
    container_session: ContainerSessionContext,
) -> None:
    """Create the non-root Claude user identity inside a running container."""
    script = (
        "set -eu\n"
        f"if ! grep -q '^[^:]*:[^:]*:{CONTAINER_AGENT_GID}:' /etc/group; then\n"
        f"  printf '%s\\n' '{CONTAINER_AGENT_USER}:x:{CONTAINER_AGENT_GID}:' "
        ">> /etc/group\n"
        "fi\n"
        f"if ! grep -q '^[^:]*:[^:]*:{CONTAINER_AGENT_UID}:' /etc/passwd; then\n"
        "  printf '%s\\n' "
        f"'{CONTAINER_AGENT_USER}:x:{CONTAINER_AGENT_UID}:{CONTAINER_AGENT_GID}:"
        f"Arise Claude User:{CONTAINER_AGENT_HOME}:/bin/bash' >> /etc/passwd\n"
        "fi\n"
        f"mkdir -p {CONTAINER_AGENT_HOME} /tmp/claude-{CONTAINER_AGENT_UID}\n"
        f"chown -R {CONTAINER_AGENT_UID}:{CONTAINER_AGENT_GID} "
        f"{CONTAINER_AGENT_HOME} /tmp/claude-{CONTAINER_AGENT_UID}\n"
        f"chmod 700 {CONTAINER_AGENT_HOME}\n"
        "install -d -m 0755 /usr/local/bin\n"
        f"if [ ! -x {CONTAINER_ROOT_HELPER} ]; then\n"
        "  cat > /tmp/arise-root.c <<'ARISE_ROOT_C'\n"
        "#include <errno.h>\n"
        "#include <stdio.h>\n"
        "#include <unistd.h>\n"
        "\n"
        "int main(int argc, char *argv[]) {\n"
        "    if (argc < 2) {\n"
        '        fputs("usage: arise-root <command> [args...]\\n", stderr);\n'
        "        return 2;\n"
        "    }\n"
        "    if (setgid(0) != 0) {\n"
        '        perror("setgid");\n'
        "        return 126;\n"
        "    }\n"
        "    if (setuid(0) != 0) {\n"
        '        perror("setuid");\n'
        "        return 126;\n"
        "    }\n"
        "    execvp(argv[1], &argv[1]);\n"
        '    perror("execvp");\n'
        "    return errno == ENOENT ? 127 : 126;\n"
        "}\n"
        "ARISE_ROOT_C\n"
        "  compiler=$(command -v cc || command -v gcc || command -v clang || true)\n"
        f'  if [ -n "$compiler" ] && "$compiler" -O2 -Wall -Wextra '
        f"-o {CONTAINER_ROOT_HELPER} /tmp/arise-root.c; then\n"
        f"    chown root:root {CONTAINER_ROOT_HELPER}\n"
        f"    chmod 4755 {CONTAINER_ROOT_HELPER}\n"
        "  elif [ -x /bin/bash ]; then\n"
        f"    cp /bin/bash {CONTAINER_ROOT_BASH}\n"
        f"    chown root:root {CONTAINER_ROOT_BASH}\n"
        f"    chmod 4755 {CONTAINER_ROOT_BASH}\n"
        f"    cat > {CONTAINER_ROOT_HELPER} <<'ARISE_ROOT_SH'\n"
        "#!/bin/sh\n"
        f'exec {CONTAINER_ROOT_BASH} -p -c \'exec "$@"\' arise-root "$@"\n'
        "ARISE_ROOT_SH\n"
        f"    chmod 0755 {CONTAINER_ROOT_HELPER}\n"
        "  else\n"
        "    echo 'no compiler or bash available to create root helper' >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  rm -f /tmp/arise-root.c\n"
        "fi\n"
        f"cat > {CONTAINER_SUDO_SHIM} <<'ARISE_SUDO_SH'\n"
        "#!/bin/sh\n"
        'if [ "$#" -eq 0 ]; then\n'
        "  echo 'usage: sudo <command> [args...]' >&2\n"
        "  exit 2\n"
        "fi\n"
        f'exec {CONTAINER_ROOT_HELPER} "$@"\n'
        "ARISE_SUDO_SH\n"
        f"chmod 0755 {CONTAINER_SUDO_SHIM}\n"
        f"cat > {CONTAINER_APT_GET_SHIM} <<'ARISE_APT_GET_SH'\n"
        "#!/bin/sh\n"
        f'exec {CONTAINER_ROOT_HELPER} /usr/bin/apt-get "$@"\n'
        "ARISE_APT_GET_SH\n"
        f"chmod 0755 {CONTAINER_APT_GET_SHIM}\n"
    )
    argv = [
        "docker",
        "exec",
        "-i",
        container_session.container_id,
        "bash",
        "-lc",
        script,
    ]
    logger.debug(
        "Preparing non-root Claude user %s in container %s",
        CONTAINER_AGENT_USER,
        container_session.container_name,
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=CONTAINER_USER_PREP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await _wait_after_kill(process)
        logger.warning(
            "Timed out preparing non-root user in container %s; continuing with "
            "worker subprocess watchdog enabled",
            container_session.container_name,
        )
        return
    if process.returncode != 0:
        logger.warning(
            "Failed to prepare non-root user in container %s: %s",
            container_session.container_name,
            output.decode("utf-8", errors="replace").strip(),
        )


async def _wait_after_kill(process: Any) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_KILL_GRACE_SECONDS)
    except TimeoutError:
        logger.warning("Timed out waiting for killed container-prep process")
