#!/usr/bin/env python3
# ruff: noqa: EXE001  # The handoff contract invokes this wrapper through python3.
"""Run or resume N1, including preparation and image provisioning."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO


if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
N1_DIR = ROOT_DIR / "experiments/n1-secbench-full"
ENV_FILE = N1_DIR / ".env"
LOG_DIR = ROOT_DIR / "_run_logs" / "n1-secbench-full"
SMOKE_TASKS = "gpac.cve-2024-0322"
_OUTPUT_CHUNK_BYTES = 64 * 1024


class RunError(RuntimeError):
    """The N1 handoff workflow cannot start."""


class _BinaryLogTextStream:
    """Adapt the run's binary log stream for ``logging.StreamHandler``."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def write(self, message: str) -> int:
        self._stream.write(message.encode("utf-8", errors="replace"))
        return len(message)

    def flush(self) -> None:
        self._stream.flush()


class _TimestampedLogWriter:
    """Prefix complete child-output lines with their host receipt time."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._pending = bytearray()

    def write(self, chunk: bytes) -> None:
        self._pending.extend(chunk)
        while (newline := self._pending.find(b"\n")) >= 0:
            line = bytes(self._pending[: newline + 1])
            del self._pending[: newline + 1]
            self._write_line(line)

    def flush(self) -> None:
        if self._pending:
            self._write_line(bytes(self._pending))
            self._pending.clear()
        self._stream.flush()

    def _write_line(self, line: bytes) -> None:
        timestamp = (
            datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            .encode("ascii")
        )
        self._stream.write(timestamp + b" " + line)
        self._stream.flush()


def _new_log_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return LOG_DIR / f"run-{timestamp}-{os.getpid()}.log"


@contextmanager
def _run_log() -> Iterator[tuple[Path, BinaryIO]]:
    """Create one append-free host log and mirror wrapper messages into it."""
    path = _new_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb", buffering=0) as stream:
        path.chmod(0o600)
        formatter = logging.Formatter(
            "%(asctime)sZ %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        file_handler = logging.StreamHandler(_BinaryLogTextStream(stream))
        file_handler.setFormatter(formatter)
        previous_handlers = list(logger.handlers)
        previous_level = logger.level
        previous_propagate = logger.propagate
        logger.handlers = [console_handler, file_handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            yield path, stream
        finally:
            logger.handlers = previous_handlers
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate
            console_handler.close()
            file_handler.close()


def _is_secret_name(name: str) -> bool:
    return name != "OPENAI_API_KEY" and name.endswith(("_KEY", "_PASSWORD", "_SECRET", "_TOKEN"))


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


def _write_process_output(chunk: bytes, log_writer: _TimestampedLogWriter) -> None:
    log_writer.write(chunk)
    terminal = getattr(sys.stdout, "buffer", None)
    if terminal is None:
        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
        sys.stdout.flush()
    else:
        terminal.write(chunk)
        terminal.flush()


def _stream_process(
    argv: list[str],
    *,
    log_stream: BinaryIO,
) -> None:
    """Run one child and mirror its combined output without changing its environment."""
    process = subprocess.Popen(  # noqa: S603
        argv,
        cwd=ROOT_DIR,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        raise RunError(f"cannot capture output from {argv[0]}")
    log_writer = _TimestampedLogWriter(log_stream)
    with process.stdout:
        try:
            while chunk := os.read(process.stdout.fileno(), _OUTPUT_CHUNK_BYTES):
                _write_process_output(chunk, log_writer)
        finally:
            log_writer.flush()
    returncode = process.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, argv)


def _run(
    script: str,
    arguments: list[str] | None = None,
    *,
    log_stream: BinaryIO,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RunError("execute experiments/n1-secbench-full/setup.py first")
    argv = [uv, "run", "python", str(N1_DIR / script), *(arguments or [])]
    _stream_process(argv, log_stream=log_stream)


def main(argv: list[str] | None = None) -> int:
    """Run or resume the colleague-facing N1 experiment."""
    arguments = list(argv if argv is not None else sys.argv[1:])
    smoke = "--smoke" in arguments
    arguments = [argument for argument in arguments if argument != "--smoke"]
    if smoke:
        arguments.extend(["--instances", SMOKE_TASKS])
    try:
        with _run_log() as (log_path, log_stream):
            logger.info("run log: %s", log_path)
            logger.info("run arguments: %s", shlex.join(arguments) if arguments else "<defaults>")
            try:
                _load_environment()
                _load_openai_key()
                logger.info("starting prepare_n1_experiment.py")
                _run("prepare_n1_experiment.py", log_stream=log_stream)
                logger.info("finished prepare_n1_experiment.py")
                logger.info("starting run_batch.py")
                _run("run_batch.py", arguments, log_stream=log_stream)
                logger.info("finished run_batch.py")
            except RunError as exc:
                logger.error("run failed: %s", exc)
                return 1
            except OSError as exc:
                logger.error("run failed: %s", exc)
                return 1
            except subprocess.CalledProcessError as exc:
                script_name = Path(exc.cmd[3]).name
                logger.error(
                    "run failed: %s exited with status %d",
                    script_name,
                    exc.returncode,
                )
                return exc.returncode
            except KeyboardInterrupt:
                logger.error("run interrupted; rerun the same command to resume")
                return 130
            logger.info("run complete: log=%s", log_path)
    except OSError as exc:
        print(f"run failed: cannot create run log: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
