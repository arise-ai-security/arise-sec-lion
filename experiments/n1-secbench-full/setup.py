#!/usr/bin/env python3
"""Install the local N1 runtime and write its study-local environment file."""

from __future__ import annotations

import getpass
import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
N1_ENV = ROOT_DIR / "experiments/n1-secbench-full/.env"
N1_ENV_EXAMPLE = ROOT_DIR / "experiments/n1-secbench-full/.env.example"
COMPOSE_ENV = ROOT_DIR / "deployment/.env"
UV_VERSION = "0.11.29"


class SetupError(RuntimeError):
    """N1 host setup could not complete."""


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> None:
    try:
        subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_text,
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SetupError(f"command failed ({exc.returncode}): {argv[0]}") from exc


def _output(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise SetupError(f"command failed ({exc.returncode}): {argv[0]}") from exc
    return result.stdout.strip()


def _succeeds(argv: list[str]) -> bool:
    return (
        subprocess.run(
            argv,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _download_text(url: str) -> str:
    try:
        with urlopen(url, timeout=60) as response:  # noqa: S310
            return response.read().decode()
    except (OSError, URLError, UnicodeError) as exc:
        raise SetupError(f"download failed: {url}") from exc


def _ensure_uv() -> str:
    installed = shutil.which("uv")
    if installed is not None:
        return installed

    logger.info("Installing uv %s...", UV_VERSION)
    installer = _download_text(f"https://astral.sh/uv/{UV_VERSION}/install.sh")
    env = os.environ.copy()
    env["UV_NO_MODIFY_PATH"] = "1"
    _run(["sh"], env=env, input_text=installer)
    local_bin = str(Path.home() / ".local/bin")
    os.environ["PATH"] = f"{local_bin}{os.pathsep}{os.environ['PATH']}"
    installed = shutil.which("uv")
    if installed is None:
        raise SetupError("uv installation did not produce ~/.local/bin/uv")
    return installed


def _ensure_homebrew() -> str:
    installed = shutil.which("brew")
    if installed is not None:
        return installed

    logger.info("Installing Homebrew...")
    installer = _download_text(
        "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    )
    _run(["/bin/bash", "-c", installer])
    for candidate in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")):
        if candidate.is_file():
            os.environ["PATH"] = f"{candidate.parent}{os.pathsep}{os.environ['PATH']}"
            return str(candidate)
    raise SetupError("Homebrew installation is not on PATH")


def _link_cli_plugin(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not target.is_symlink():
        return
    target.unlink(missing_ok=True)
    target.symlink_to(source)


def _install_macos_container_runtime() -> None:
    brew = _ensure_homebrew()
    logger.info("Installing the local container runtime...")
    _run([brew, "install", "docker", "docker-compose", "colima"])
    compose = Path(_output([brew, "--prefix", "docker-compose"])) / "bin/docker-compose"
    _link_cli_plugin(compose, Path.home() / ".docker/cli-plugins/docker-compose")
    colima = shutil.which("colima")
    if colima is None:
        raise SetupError("Colima installation is not on PATH")
    _run([colima, "start", "--arch", "x86_64", "--cpu", "4", "--memory", "8"])


def _install_linux_container_runtime() -> None:
    installer = _download_text("https://get.docker.com")
    installer_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix="get-docker-", delete=False) as stream:
            stream.write(installer)
            installer_path = Path(stream.name)
        _run(["sudo", "sh", str(installer_path)])
    finally:
        if installer_path is not None:
            installer_path.unlink(missing_ok=True)
    _run(["sudo", "systemctl", "enable", "--now", "docker"])
    docker = shutil.which("docker")
    if docker is None or not _succeeds([docker, "info"]):
        user = os.environ.get("USER")
        if not user:
            raise SetupError("USER is required to configure Docker permissions")
        _run(["sudo", "usermod", "-aG", "docker", user])
        raise SetupError("sign out once, sign in, and rerun setup.py")


def _ensure_container_runtime() -> str:
    docker = shutil.which("docker")
    system = platform.system()
    if docker is None:
        if system == "Darwin":
            _install_macos_container_runtime()
        elif system == "Linux":
            _install_linux_container_runtime()
        else:
            raise SetupError("only macOS and Linux are supported")
        docker = shutil.which("docker")
    if docker is None:
        raise SetupError("container runtime installation did not produce docker")

    if not _succeeds([docker, "info"]):
        colima = shutil.which("colima")
        if system == "Darwin" and colima is not None:
            _run([colima, "start", "--arch", "x86_64", "--cpu", "4", "--memory", "8"])
        elif system == "Darwin" and Path("/Applications/Docker.app").is_dir():
            _run(["open", "-ga", "Docker"])
        elif system == "Linux":
            _run(["sudo", "systemctl", "start", "docker"])

    for _ in range(60):
        if _succeeds([docker, "info"]):
            break
        time.sleep(2)
    if not _succeeds([docker, "info"]):
        raise SetupError("the container runtime did not become ready")
    if not _succeeds([docker, "compose", "version"]):
        raise SetupError("the Compose plugin is unavailable")
    return docker


def _database_port(docker: str) -> str:
    result = subprocess.run(
        [docker, "port", "postgres-main", "5432/tcp"],
        check=False,
        capture_output=True,
        text=True,
    )
    binding = result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else ""
    return binding.rsplit(":", maxsplit=1)[-1] if binding else "55432"


def _write_environment(docker: str) -> None:
    if not N1_ENV.exists():
        N1_ENV.parent.mkdir(parents=True, exist_ok=True)
        example_lines = N1_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        replacements = {
            "HOST_PROJECT_ROOT": str(ROOT_DIR),
            "POSTGRES_PORT": _database_port(docker),
        }
        environment_lines = []
        for line in example_lines:
            name = line.split("=", maxsplit=1)[0].strip()
            value = replacements.get(name)
            environment_lines.append(f"{name}={shlex.quote(value)}" if value else line)
        N1_ENV.write_text("\n".join(environment_lines) + "\n", encoding="utf-8")
        N1_ENV.chmod(0o600)
    compose_source = COMPOSE_ENV if COMPOSE_ENV.exists() else N1_ENV
    original_compose_lines = compose_source.read_text(encoding="utf-8").splitlines()
    compose_lines = [
        line
        for line in original_compose_lines
        if not line.lstrip().startswith("OPENAI_API_KEY=")
    ]
    if not COMPOSE_ENV.exists() or compose_lines != original_compose_lines:
        COMPOSE_ENV.write_text("\n".join(compose_lines) + "\n", encoding="utf-8")
        COMPOSE_ENV.chmod(0o600)


def _environment_value(name: str) -> str | None:
    environment_lines = N1_ENV.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(environment_lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate, raw_value = line.split("=", maxsplit=1)
        if candidate.strip() != name:
            continue
        if not raw_value.strip():
            return ""
        values = shlex.split(raw_value, posix=True)
        if len(values) != 1:
            raise SetupError(f"invalid {name} value on .env line {line_number}")
        return values[0]
    return None


def _write_environment_value(name: str, value: str) -> None:
    replacement = f"{name}={shlex.quote(value)}"
    lines = N1_ENV.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if "=" in line and line.split("=", maxsplit=1)[0].strip() == name:
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    N1_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    N1_ENV.chmod(0o600)


def _ensure_openai_key() -> None:
    if _environment_value("OPENAI_API_KEY"):
        return
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        if not sys.stdin.isatty():
            raise SetupError("OPENAI_API_KEY is required; rerun setup.py in a terminal")
        key = getpass.getpass("OpenAI API key: ")
    if not key:
        raise SetupError("OPENAI_API_KEY is empty")
    _write_environment_value("OPENAI_API_KEY", key)


def main() -> int:
    """Prepare a cloned repository for the N1 experiment."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        uv = _ensure_uv()
        docker = _ensure_container_runtime()
        _write_environment(docker)
        _ensure_openai_key()
        logger.info("Installing the locked project dependencies...")
        _run([uv, "sync", "--frozen"], cwd=ROOT_DIR)
    except (OSError, SetupError, ValueError) as exc:
        logger.error("setup failed: %s", exc)
        return 1
    logger.info("Setup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
