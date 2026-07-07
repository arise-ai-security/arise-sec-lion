"""Parent-side ``main.py run`` subprocess invocation and supervision.

Builds the exact ``python main.py -c <config> run <task>`` argv the harness uses
and runs it under async supervision: on timeout or cancellation it terminates the
child's process tree and removes that pid's labeled Docker containers (via
:mod:`experiments.shared.container_cleanup`). The harness stays a thin coordinator
that calls :func:`_invoke_main_py` / :func:`_invoke_main_py_async`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from experiments.shared.container_cleanup import (
    _cleanup_labeled_containers_for_pid,
    _terminate_process_tree,
)
from experiments.shared.scripts._paths import get_repo_root


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

# The harness passes a unique result-file path to each subprocess via this env
# var so concurrent `main.py run` invocations cannot misattribute each other's
# run_id. See `presentation/persistence/run_persistence.py:RUN_RESULT_ENV_VAR`.
RUN_RESULT_ENV_VAR = "ARISE_RUN_RESULT_PATH"
_SUBPROCESS_TIMEOUT_EXIT_CODE = 124
_SUBPROCESS_TERM_GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class MainPyInvocation:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: float


def _subprocess_timeout_seconds() -> float:
    return float(os.environ.get("ARISE_SUBPROCESS_TIMEOUT_SECONDS", "7800"))


def _subprocess_term_grace_seconds() -> float:
    return float(
        os.environ.get("ARISE_SUBPROCESS_TERM_GRACE_SECONDS", _SUBPROCESS_TERM_GRACE_SECONDS)
    )


def _build_main_py_invocation(
    *,
    config: Path,
    task: str,
    context_file: Path,
    result_path: Path,
    python_bin: str | None = None,
) -> MainPyInvocation:
    """Build the exact ``main.py run`` invocation used by the harness.

    `-c` MUST come BEFORE the `run` subcommand (top-level flag, see
    bootstrap/bootstrap.py:67). The subprocess inherits ``ARISE_RUN_RESULT_PATH``
    pointing at ``result_path`` so the CLI can emit a per-invocation
    ``{boss_id, status}`` payload there.
    """
    python = python_bin or sys.executable
    main_py = get_repo_root() / "main.py"
    cmd = [
        python,
        str(main_py),
        "-c",
        str(config),
        "run",
        task,
        "--domain",
        "security",
        "--domain-context-file",
        str(context_file),
    ]
    env = {**os.environ, RUN_RESULT_ENV_VAR: str(result_path)}
    return MainPyInvocation(
        cmd=cmd,
        cwd=get_repo_root(),
        env=env,
        timeout_seconds=_subprocess_timeout_seconds(),
    )


def _invoke_main_py(
    *,
    config: Path,
    task: str,
    context_file: Path,
    result_path: Path,
    python_bin: str | None = None,
) -> int:
    """Run ``python main.py -c <config> run <task>`` and return its exit code."""
    return asyncio.run(
        _invoke_main_py_async(
            config=config,
            task=task,
            context_file=context_file,
            result_path=result_path,
            python_bin=python_bin,
        )
    )


async def _invoke_main_py_async(
    *,
    config: Path,
    task: str,
    context_file: Path,
    result_path: Path,
    python_bin: str | None = None,
) -> int:
    """Run ``main.py run`` under parent-side async process supervision."""
    invocation = _build_main_py_invocation(
        config=config,
        task=task,
        context_file=context_file,
        result_path=result_path,
        python_bin=python_bin,
    )
    cmd = invocation.cmd
    logger.info("invoking: %s", " ".join(cmd))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=invocation.cwd,
        env=invocation.env,
        start_new_session=True,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=invocation.timeout_seconds)
    except TimeoutError:
        logger.error(
            "subprocess timed out after %.0fs: %s",
            invocation.timeout_seconds,
            " ".join(cmd),
        )
        await _terminate_process_tree(
            process,
            reason="timeout",
            grace_seconds=_subprocess_term_grace_seconds(),
        )
        await _cleanup_labeled_containers_for_pid(process.pid)
        return _SUBPROCESS_TIMEOUT_EXIT_CODE
    except asyncio.CancelledError:
        logger.warning("subprocess dispatch cancelled; terminating: %s", " ".join(cmd))
        await _terminate_process_tree(
            process,
            reason="cancellation",
            grace_seconds=_subprocess_term_grace_seconds(),
        )
        await _cleanup_labeled_containers_for_pid(process.pid)
        raise
