#!/usr/bin/env python3
"""Run or resume N1, including preparation and image provisioning."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
N1_DIR = ROOT_DIR / "experiments/n1-secbench-full"
ENV_FILE = N1_DIR / ".env"
SMOKE_TASKS = "gpac.cve-2024-0322"


class RunError(RuntimeError):
    """The N1 handoff workflow cannot start."""


def _is_secret_name(name: str) -> bool:
    return name != "OPENAI_API_KEY" and name.endswith(
        ("_KEY", "_PASSWORD", "_SECRET", "_TOKEN")
    )


def _load_environment() -> None:
    if not ENV_FILE.is_file():
        raise RunError("execute experiments/n1-secbench-full/setup.py first")
    environment_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(environment_lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RunError(f"invalid .env line {line_number}")
        name, raw_value = line.split("=", maxsplit=1)
        name = name.strip()
        if _is_secret_name(name):
            raise RunError(".env permits only N1 host settings and OPENAI_API_KEY")
        if raw_value.strip():
            values = shlex.split(raw_value, posix=True)
            if len(values) != 1:
                raise RunError(f"invalid .env value on line {line_number}")
            value = values[0]
        else:
            value = ""
        os.environ.setdefault(name, value)


def _load_openai_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    raise RunError("OPENAI_API_KEY is missing from experiments/n1-secbench-full/.env")


def _run(script: str, arguments: list[str] | None = None) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RunError("execute experiments/n1-secbench-full/setup.py first")
    argv = [uv, "run", "python", str(N1_DIR / script), *(arguments or [])]
    subprocess.run(argv, cwd=ROOT_DIR, check=True)


def main(argv: list[str] | None = None) -> int:
    """Run or resume the colleague-facing N1 experiment."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    arguments = list(argv if argv is not None else sys.argv[1:])
    smoke = "--smoke" in arguments
    arguments = [argument for argument in arguments if argument != "--smoke"]
    if smoke:
        arguments.extend(["--instances", SMOKE_TASKS])
    try:
        _load_environment()
        _load_openai_key()
        _run("prepare_n1_experiment.py")
        _run("run_batch.py", arguments)
    except RunError as exc:
        logger.error("run failed: %s", exc)
        return 1
    except subprocess.CalledProcessError as exc:
        script_name = Path(exc.cmd[3]).name
        logger.error("run failed: %s exited with status %d", script_name, exc.returncode)
        return exc.returncode
    except KeyboardInterrupt:
        logger.error("run interrupted; rerun the same command to resume")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
