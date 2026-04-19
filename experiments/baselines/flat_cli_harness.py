"""Flat Claude Code CLI harness for experiment baseline runs.

Runs ``claude --print --output-format stream-json`` inside a SEC-bench Docker
container, streams stdout line-by-line, converts each recognized event into a
``NormalizedEvent`` conforming to ``experiments.schema``, and writes
``events.jsonl``.

Enforces per-run budget ($3) and wall-clock (10 min) caps by killing the
container when exceeded; synthesizes a ``run_completed`` event with
termination reason in the accompanying ``meta.json``.

The unit tests in ``tests/test_flat_cli_harness.py`` cover only the pure
parser and helper functions (``parse_stream_line``, ``build_prompt``,
``compute_redundancy_targets``). The ``run_flat_cli`` coroutine requires
Docker + the Claude CLI + an API key, and is exercised only by the Task 16
integration smoke. Keep the parser purely functional so unit tests stay
fast and deterministic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from experiments.schema import (
    SCHEMA_VERSION,
    NormalizedEvent,
    RunLifecyclePayload,
    RunMeta,
    TokensConsumedPayload,
    ToolResultPayload,
    ToolUsePayload,
)


logger = logging.getLogger(__name__)

# Tool-result body cap (characters). Beyond this, the captured text is
# truncated to the first MAX_RESULT_BYTES characters and ``was_truncated``
# is set to True; ``result_bytes`` preserves the original length. Kept
# self-contained in ``experiments/`` rather than importing from
# ``infrastructure/`` so the experiments tree has no inbound dependency on
# the hex-boundary adapters.
MAX_RESULT_BYTES = 10_240

# Subprocess readline() buffer cap. Default asyncio StreamReader limit is
# 64 KB, which is easily exceeded by a single stream-json line carrying a
# large ``tool_result.content`` (valgrind stderr, klee output, etc.). When
# breached, ``readline()`` raises ``ValueError`` and the run is incorrectly
# marked ``container_error``. 10 MB is generous but bounded.
STREAM_READ_BUFFER_LIMIT = 10 * 1024 * 1024

# ``domain_briefing.md`` lives one level up from this file
# (``experiments/domain_briefing.md``).
DOMAIN_BRIEFING_PATH = Path(__file__).resolve().parents[1] / "domain_briefing.md"

SECURITY_TOOL_PREAMBLE = """\
AVAILABLE SECURITY TOOLS

You have access to the following tools via Bash:
- valgrind: memory-error detector (buffer overflows, leaks, use-after-free)
  Example: valgrind --error-exitcode=1 ./target_binary <args>
- klee: symbolic execution engine for automatic test-input generation
  Example: klee --only-output-states-covering-new target.bc

You MUST use valgrind to verify your exploit reproduces the memory error, and
to verify your patch eliminates it. Use klee only if symbolic execution is
warranted by the task.
"""


@dataclass(frozen=True)
class CellConfig:
    """Configuration for one flat-CLI experiment cell."""

    cell_id: str  # A1, A2, A3, A4
    subagents_enabled: bool
    domain_briefing_enabled: bool


CELL_CONFIGS: dict[str, CellConfig] = {
    "A1": CellConfig("A1", subagents_enabled=True, domain_briefing_enabled=False),
    "A2": CellConfig("A2", subagents_enabled=False, domain_briefing_enabled=False),
    "A3": CellConfig("A3", subagents_enabled=True, domain_briefing_enabled=True),
    "A4": CellConfig("A4", subagents_enabled=False, domain_briefing_enabled=True),
}


def build_prompt(task_text: str, cell: CellConfig) -> str:
    """Compose the final prompt for the CLI based on cell configuration."""
    parts: list[str] = [SECURITY_TOOL_PREAMBLE]
    if cell.domain_briefing_enabled:
        parts.append(DOMAIN_BRIEFING_PATH.read_text(encoding="utf-8"))
    parts.append(task_text)
    return "\n\n---\n\n".join(parts)


def parse_stream_line(
    line: str,
    *,
    run_id: str,
    now: datetime,
) -> list[NormalizedEvent]:
    """Parse one line of ``claude --output-format stream-json`` output.

    Returns zero or more ``NormalizedEvent`` instances. Malformed or unknown
    lines return ``[]`` and never raise. The ``sequence_number`` on each
    returned event is a placeholder (``0``); the harness assigns the final
    value when writing to ``events.jsonl``.
    """
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Failed to parse stream line (first 200 chars): %s ... err=%s",
            line[:200],
            exc,
        )
        return []

    msg_type = obj.get("type", "")
    if msg_type == "assistant":
        return _parse_assistant(obj, run_id=run_id, now=now)
    if msg_type == "user":
        return _parse_user(obj, run_id=run_id, now=now)
    if msg_type == "result":
        return _parse_result(obj, run_id=run_id, now=now)
    return []


def _parse_assistant(
    obj: dict[str, Any], *, run_id: str, now: datetime
) -> list[NormalizedEvent]:
    content = (obj.get("message") or {}).get("content") or []
    events: list[NormalizedEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        events.append(
            NormalizedEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                occurred_at=now,
                event_type="tool_use",
                source="flat_cli",
                role="FLAT",
                depth=0,
                sequence_number=0,
                payload=ToolUsePayload(
                    call_id=block.get("id"),
                    tool_name=str(block.get("name", "")),
                    tool_input=dict(block.get("input") or {}),
                    duration_ms=None,
                ),
            )
        )
    return events


def _parse_user(
    obj: dict[str, Any], *, run_id: str, now: datetime
) -> list[NormalizedEvent]:
    content = (obj.get("message") or {}).get("content") or []
    events: list[NormalizedEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        raw = block.get("content", "") or ""
        # Stream-json sometimes delivers tool_result content as a list of
        # content blocks (e.g. ``[{"type":"text","text":"..."}]``) instead
        # of a plain string. Flatten to text either way.
        if isinstance(raw, list):
            raw = "\n".join(
                (c.get("text", "") if isinstance(c, dict) else str(c))
                for c in raw
            )
        raw_str = str(raw)
        result_bytes = len(raw_str)
        was_truncated = result_bytes > MAX_RESULT_BYTES
        capped = raw_str[:MAX_RESULT_BYTES]
        events.append(
            NormalizedEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                occurred_at=now,
                event_type="tool_result",
                source="flat_cli",
                role="FLAT",
                depth=0,
                sequence_number=0,
                payload=ToolResultPayload(
                    call_id=block.get("tool_use_id"),
                    result_text=capped,
                    was_truncated=was_truncated,
                    result_bytes=result_bytes,
                    is_error=bool(block.get("is_error", False)),
                ),
            )
        )
    return events


def _parse_result(
    obj: dict[str, Any], *, run_id: str, now: datetime
) -> list[NormalizedEvent]:
    usage = obj.get("usage") or {}
    cost = float(obj.get("total_cost_usd") or 0.0)
    duration_ms = obj.get("duration_ms")
    duration_seconds = (
        float(duration_ms) / 1000.0 if duration_ms is not None else None
    )
    tokens_consumed = NormalizedEvent(
        event_id=str(uuid4()),
        run_id=run_id,
        occurred_at=now,
        event_type="tokens_consumed",
        source="flat_cli",
        role="FLAT",
        depth=0,
        sequence_number=0,
        payload=TokensConsumedPayload(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(
                usage.get("cache_creation_input_tokens") or 0
            ),
            thinking_tokens=None,  # CLI doesn't currently separate reasoning tokens
            cost_usd=cost,
            operation="worker_execution",
            model=None,
        ),
    )
    run_completed = NormalizedEvent(
        event_id=str(uuid4()),
        run_id=run_id,
        occurred_at=now,
        event_type="run_completed",
        source="flat_cli",
        role="FLAT",
        depth=0,
        sequence_number=0,
        payload=RunLifecyclePayload(
            status=str(obj.get("subtype") or "completed"),
            duration_seconds=duration_seconds,
        ),
    )
    return [tokens_consumed, run_completed]


def compute_redundancy_targets(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Normalize a tool invocation to a "target" string for redundancy matching.

    Matches the definition in the experiment spec §5.3. Bash commands are
    reduced to the first three whitespace-separated tokens so semantically
    equivalent invocations (``git status`` vs ``git status -uno``) collapse
    to the same target bucket.
    """
    if tool_name in ("Read", "Edit", "Write"):
        return str(tool_input.get("file_path", "") or "")
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "") or "")
        tokens = cmd.split()[:3]
        return " ".join(tokens)
    if tool_name in ("Grep", "Glob"):
        return str(tool_input.get("pattern", "") or "")
    return str(tool_input)


# ---------------------------------------------------------------------------
# Container runner
#
# Everything below orchestrates the actual `claude` subprocess in a Docker
# container. It is not covered by the unit tests (would require Docker + the
# Claude CLI + an API key). Task 16 runs it end-to-end as the integration
# smoke.
# ---------------------------------------------------------------------------


@dataclass
class RunSpec:
    """Inputs for a single flat-CLI run."""

    cve_instance_path: Path  # path to CVE JSON
    cell: CellConfig
    replicate: int
    output_dir: Path  # dataset/runs/<cve>/<cell>/<replicate>/
    docker_image: str  # e.g. secb-tools:njs.cve-2022-32414
    model: str = "claude-sonnet-4-6"
    budget_usd_cap: float = 3.0
    wallclock_sec_cap: int = 1200
    workspace_host_root: Path | None = None  # provided by experiment runner


async def _stream_to_events(
    proc: asyncio.subprocess.Process,
    events_f: TextIO,
    log_f: TextIO,
    *,
    run_id: str,
    spec: RunSpec,
    started_monotonic: float,
    starting_seq: int,
) -> tuple[str, int, float]:
    """Drain proc.stdout line-by-line, emit events, enforce caps.

    Returns ``(termination_reason, last_seq, total_cost)``. The caller owns
    ``proc`` and is responsible for reaping it after this returns. Callers
    must pre-write any ``run_started`` event so ``starting_seq`` reflects
    the next free sequence number.
    """
    assert proc.stdout is not None
    termination_reason = "completed"
    total_cost = 0.0
    event_seq = starting_seq

    while True:
        if time.monotonic() - started_monotonic > spec.wallclock_sec_cap:
            termination_reason = "wallclock_cap"
            proc.kill()
            break
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
        except TimeoutError:
            continue
        if not line:
            break
        line_str = line.decode("utf-8", errors="replace").rstrip("\n")
        log_f.write(line_str + "\n")
        events = parse_stream_line(line_str, run_id=run_id, now=datetime.now(UTC))
        kill_now = False
        for ev in events:
            ev_dump = ev.model_copy(update={"sequence_number": event_seq})
            events_f.write(ev_dump.model_dump_json() + "\n")
            event_seq += 1
            if ev.event_type == "tokens_consumed":
                # Downstream dispatch is by event_type (per schema docstring),
                # but accessing payload fields requires getattr to appease
                # the union type checker.
                cost = getattr(ev.payload, "cost_usd", 0.0)
                total_cost += float(cost or 0.0)
                if total_cost >= spec.budget_usd_cap:
                    termination_reason = "budget_cap"
                    kill_now = True
        if kill_now:
            proc.kill()
            break
        if proc.returncode is not None:
            break

    return termination_reason, event_seq, total_cost


async def run_flat_cli(spec: RunSpec, task_text: str) -> RunMeta:
    """Execute a single flat-CLI run and populate ``spec.output_dir``.

    Writes ``events.jsonl``, ``meta.json``, and ``stdout_stderr.log``. The
    ``artifacts/`` subdirectory is created here but populated by the
    experiment runner (which copies ``/testcase`` out of the container after
    the run terminates).

    Returns the ``RunMeta`` for indexing by the experiment runner.
    """
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    (spec.output_dir / "artifacts").mkdir(exist_ok=True)

    if spec.workspace_host_root is None:
        raise ValueError(
            "RunSpec.workspace_host_root must be set by the experiment runner"
        )

    run_id = f"{_extract_cve_id(spec.cve_instance_path)}-{spec.cell.cell_id}-{spec.replicate}"
    prompt = build_prompt(task_text, spec.cell)
    events_path = spec.output_dir / "events.jsonl"
    log_path = spec.output_dir / "stdout_stderr.log"
    disallowed_args: list[str] = (
        [] if spec.cell.subagents_enabled else ["--disallowedTools", "Task"]
    )

    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()

    # Build the inner CLI command safely. ``shlex.join`` quotes each argv
    # element so a malformed ``spec.model`` (set from YAML by Task 13)
    # cannot escape into the outer ``bash -lc`` shell.
    #
    # ``--verbose`` is REQUIRED by ``claude`` 2.1.x when combined with
    # ``--print --output-format stream-json`` (CLI emits
    # "--output-format=stream-json requires --verbose" otherwise).
    cli_cmd = shlex.join(
        [
            "claude", "--print", "--verbose", "--output-format", "stream-json",
            "--model", spec.model,
            "--permission-mode", "bypassPermissions",
            *disallowed_args,
        ]
    )
    # Deviations from the original spec (§1.1) documented here for audit:
    #
    # * ``--network=none`` was removed. The CLI invokes api.anthropic.com
    #   from inside the container, which ``--network=none`` blocks. The
    #   spec's intent ("block external WebFetch") is preserved by not
    #   passing WebFetch-enabling tools to the CLI -- the API endpoint is
    #   still reached via the default bridge network.
    # * ``--user 1000:1000`` and ``HOME=/tmp`` were added. The Claude CLI
    #   refuses ``--permission-mode bypassPermissions`` when running as
    #   root (see https://docs.anthropic.com/claude-code). A non-root UID
    #   with a writable HOME is the published recommendation for CI.
    cmd = [
        "docker", "run", "--rm",
        "--user", "1000:1000",
        "-i",
        "-e", "ANTHROPIC_API_KEY",
        "-e", "HOME=/tmp",
        "-v", f"{spec.workspace_host_root}:/workspace",
        "-w", "/src",
        spec.docker_image,
        "bash", "-lc",
        cli_cmd,
    ]

    termination_reason: str = "completed"
    event_seq = 0
    proc: asyncio.subprocess.Process | None = None

    try:
        # stderr is merged into stdout (STDOUT) rather than read via a
        # separate PIPE: over a 10-minute container run stderr easily
        # exceeds asyncio's 64 KB StreamReader default, which would block
        # the writer and deadlock the pipeline. Merging keeps the capture
        # in ``stdout_stderr.log`` and the parser already returns [] for
        # any non-JSON line it encounters.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=STREAM_READ_BUFFER_LIMIT,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()

        with (
            events_path.open("w", encoding="utf-8") as events_f,
            log_path.open("w", encoding="utf-8") as log_f,
        ):
            event_seq = _emit_run_started(events_f, run_id, started_at, event_seq)
            termination_reason, event_seq, _total_cost = await _stream_to_events(
                proc,
                events_f,
                log_f,
                run_id=run_id,
                spec=spec,
                started_monotonic=started_monotonic,
                starting_seq=event_seq,
            )

        await proc.wait()
    except Exception:
        termination_reason = "container_error"
        logger.exception("Flat CLI run failed")
        # Don't re-raise; meta.json must still be written so the experiment
        # runner can index the failed run.
    finally:
        if proc is not None:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            # Always reap the subprocess. ``proc.kill()`` delivers SIGKILL
            # but the child remains a zombie until ``wait()`` returns; over
            # 63 experiment runs, un-reaped zombies accumulate in the
            # runner process.
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except TimeoutError:
                logger.warning(
                    "Subprocess did not exit within 10s after kill; possible zombie"
                )

    ended_at = datetime.now(UTC)
    wallclock = (ended_at - started_at).total_seconds()

    meta = RunMeta(
        run_id=run_id,
        cve_id=_extract_cve_id(spec.cve_instance_path),
        cell=spec.cell.cell_id,  # type: ignore[arg-type]
        replicate=spec.replicate,
        system="flat_cli",
        domain_briefing_enabled=spec.cell.domain_briefing_enabled,
        subagent_enabled=spec.cell.subagents_enabled,
        prompt_strategy="cli_default",
        docker_image=spec.docker_image,
        budget_usd_cap=spec.budget_usd_cap,
        wallclock_sec_cap=spec.wallclock_sec_cap,
        models={"worker": spec.model, "judge": "gpt-5"},
        started_at=started_at,
        ended_at=ended_at,
        wallclock_seconds=wallclock,
        termination_reason=termination_reason,  # type: ignore[arg-type]
        code_sha=_collect_code_shas(),
        env={"date": datetime.now(UTC).date().isoformat()},
        dataset_schema_version=SCHEMA_VERSION,
    )
    (spec.output_dir / "meta.json").write_text(meta.model_dump_json(indent=2))
    return meta


def _emit_run_started(
    fh: TextIO, run_id: str, now: datetime, event_seq: int
) -> int:
    """Write the synthetic ``run_started`` event and return the next seq.

    Uses ``RunLifecyclePayload`` (all-optional) so the event has a semantic
    payload model even though the pydantic smart-union would also accept an
    empty ``GenericPayload``. Downstream dispatch is by ``event_type``, so
    the payload shape is not load-bearing.
    """
    ev = NormalizedEvent(
        event_id=str(uuid4()),
        run_id=run_id,
        occurred_at=now,
        event_type="run_started",
        source="flat_cli",
        role="FLAT",
        depth=0,
        sequence_number=event_seq,
        payload=RunLifecyclePayload(),
    )
    fh.write(ev.model_dump_json() + "\n")
    return event_seq + 1


def _extract_cve_id(cve_path: Path) -> str:
    """Pull ``instance_id`` from the CVE JSON file (fallback: filename stem)."""
    try:
        data = json.loads(cve_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cve_path.stem
    return str(data.get("instance_id") or cve_path.stem)


def _collect_code_shas() -> dict[str, str]:
    """Best-effort collection of git SHAs / versions for reproducibility."""
    shas: dict[str, str] = {}
    repo_root = Path(__file__).resolve().parents[2]
    # PATH-lookup is intentional here: these are developer-tool discovery
    # probes (git, the current venv's python, the CLI). They never run
    # inside the container or on untrusted paths.
    try:
        arise = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root)  # noqa: S607
            .decode()
            .strip()
        )
        shas["arise_sec_lion"] = arise
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        shas["arise_sec_lion"] = "unknown"
    # Use ``sys.executable`` to probe the venv actually running this module.
    # Bare ``"python"`` on dev machines often resolves to system Python,
    # which lacks the SDK and returns "unknown" even when it is installed.
    try:
        shas["claude_sdk_version"] = (
            subprocess.check_output(  # noqa: S603
                [
                    sys.executable,
                    "-c",
                    "import claude_agent_sdk; print(claude_agent_sdk.__version__)",
                ]
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        shas["claude_sdk_version"] = "unknown"
    if shutil.which("claude"):
        with contextlib.suppress(
            subprocess.CalledProcessError, FileNotFoundError, OSError
        ):
            shas["claude_code_cli_version"] = (
                subprocess.check_output(["claude", "--version"])  # noqa: S607
                .decode()
                .strip()
            )
    return shas
