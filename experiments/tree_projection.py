"""Project a tree-arm run's event-store events into the normalized dataset schema.

Called by ``experiments/run_experiment.py`` after each tree-cell dispatch.
Reads ``<run-dir>/.last_run.json`` (written by the CLI's RunPersistence) to
find the BOSS agent UUID, connects to the Postgres event store, pulls every
event in the agent hierarchy via recursive CTE, and converts each domain
event into a ``NormalizedEvent`` line in ``<run-dir>/events.jsonl``.

Also updates / writes ``<run-dir>/meta.json`` (a ``RunMeta``) with fields
derived from the run: run_id, cve_id, cell, replicate, models, timing,
termination reason.

Unknown event types are written as ``GenericPayload`` so the projection
never loses an event silently; structural issues log warnings.

Usage:
    uv run python -m experiments.tree_projection --run-dir <path>
    # optional: --config <path> to override default Settings path
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from typing import cast

from config.settings import Settings
from core.domain.events.events import DomainEvent
from experiments.schema import (
    SCHEMA_VERSION,
    AgentCreatedPayload,
    ChildSpawnedPayload,
    GenericPayload,
    IndexEntry,
    NormalizedEvent,
    Operation,
    PromptSentPayload,
    RetryScheduledPayload,
    Role,
    RunLifecyclePayload,
    RunMeta,
    StatusChangedPayload,
    SubtasksDefinedPayload,
    TokensConsumedPayload,
    ToolResultPayload,
    ToolUsePayload,
    VerificationFailedPayload,
    WorkerCostRecordedPayload,
)
from infrastructure.adapters.postgres_event_store import PostgresEventStore

logger = logging.getLogger(__name__)


# Maps the domain's lower-case "operation" strings on TokensConsumed /
# PromptSent to the normalized-schema Operation literals. Keep in sync with
# the OperationType alias in core/domain/services/config_resolver.py and
# every `op = "..."` / `operation=` call site in core/application/.
OPERATION_MAP: dict[str, str] = {
    "complexity_evaluation": "assess",
    "task_assessment": "assess",
    "task_decomposition": "decompose",
    "worker_execution": "worker_execution",
    "verification": "verification",
    "context_condense": "context_condense",
}


def _map_operation(raw: str) -> str:
    """Translate a domain operation string to the normalized-schema Operation literal."""
    return OPERATION_MAP.get(raw, raw)


_VALID_ROLES: frozenset[str] = frozenset(
    {"BOSS", "MANAGER", "WORKER", "PENDING", "JUDGE", "CONDENSER", "FLAT"}
)


def _map_role(raw: str) -> Role:
    """Uppercase the domain role string and validate against the schema's Role literal."""
    upper = (raw or "").upper() or "PENDING"
    if upper not in _VALID_ROLES:
        upper = "PENDING"
    return cast(Role, upper)


def _ensure_aware(dt: datetime) -> datetime:
    """Force UTC-aware datetime (AwareDatetime schema requires tzinfo)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------- #
# Agent-state accumulator
# --------------------------------------------------------------------------- #


class _AgentState:
    """Accumulates per-agent state as events are walked in order.

    Used to populate ``NormalizedEvent.role``, ``parent_agent_id``, and ``depth``
    on every emitted event. Events arrive grouped per aggregate_id (see
    ``PostgresEventStore.get_hierarchy_events_grouped``); walking chronologically
    builds up role over time (PENDING -> WORKER/MANAGER after ComplexityEvaluated).
    """

    __slots__ = ("agent_id", "role", "parent_id", "depth", "task_description")

    def __init__(
        self,
        agent_id: UUID,
        role: Role,
        parent_id: UUID | None,
        depth: int,
        task_description: str | None = None,
    ) -> None:
        self.agent_id: UUID = agent_id
        self.role: Role = role
        self.parent_id: UUID | None = parent_id
        self.depth: int = depth
        self.task_description: str | None = task_description


def _build_agent_index(
    grouped: dict[UUID, list[DomainEvent]],
    root_id: UUID,
) -> dict[UUID, _AgentState]:
    """Pre-scan all events to build the agent-state map.

    Requires AgentCreated on every agent (asserted); ComplexityEvaluated may
    update the role from PENDING to WORKER/MANAGER.
    """
    states: dict[UUID, _AgentState] = {}

    # First pass: AgentCreated for every agent; record role + parent.
    for agent_id, events in grouped.items():
        created: DomainEvent | None = None
        for ev in events:
            if type(ev).__name__ == "AgentCreated":
                created = ev
                break
        if created is None:
            logger.warning(
                "agent %s has no AgentCreated event; defaulting role=PENDING parent=None",
                agent_id,
            )
            states[agent_id] = _AgentState(
                agent_id=agent_id, role=cast(Role, "PENDING"), parent_id=None, depth=0
            )
            continue
        role = getattr(created, "role", "pending") or "pending"
        parent_id = getattr(created, "parent_id", None)
        states[agent_id] = _AgentState(
            agent_id=agent_id,
            role=_map_role(role),
            parent_id=parent_id,
            depth=0,  # filled below
        )

    # Second pass: ComplexityEvaluated promotes PENDING -> WORKER/MANAGER.
    for agent_id, events in grouped.items():
        for ev in events:
            if type(ev).__name__ == "ComplexityEvaluated":
                determined_role = getattr(ev, "determined_role", None) or getattr(
                    ev, "role", None
                )
                if determined_role:
                    states[agent_id].role = _map_role(determined_role)

    # Third pass: TaskAssigned captures the task description for AgentCreatedPayload.
    for agent_id, events in grouped.items():
        for ev in events:
            if type(ev).__name__ == "TaskAssigned":
                task = getattr(ev, "task_description", None)
                if isinstance(task, str):
                    states[agent_id].task_description = task
                break

    # Fourth pass: depth from root via parent_id chain (iterative, caps to prevent loops).
    def _compute_depth(agent_id: UUID, cap: int = 50) -> int:
        depth = 0
        current: UUID | None = agent_id
        seen: set[UUID] = set()
        while current is not None and depth < cap:
            if current == root_id:
                return depth
            if current in seen:
                logger.warning("parent-chain cycle detected at %s", current)
                return depth
            seen.add(current)
            state = states.get(current)
            if state is None:
                return depth
            current = state.parent_id
            depth += 1
        return depth

    for agent_id in states:
        states[agent_id].depth = _compute_depth(agent_id)

    return states


# --------------------------------------------------------------------------- #
# Event-type -> NormalizedEvent mapping
# --------------------------------------------------------------------------- #


def _convert_event(
    ev: DomainEvent,
    state: _AgentState,
    run_id: str,
    sequence_number_override: int,
) -> NormalizedEvent | None:
    """Convert a single domain event to its normalized form.

    Returns None for event types that add no analytical signal (metadata-only,
    or events the normalized schema doesn't model).
    """
    name = type(ev).__name__
    occurred_at = _ensure_aware(getattr(ev, "occurred_at", datetime.now(UTC)))
    agent_id = str(ev.aggregate_id)
    base = {
        "event_id": str(getattr(ev, "event_id", uuid4())),
        "run_id": run_id,
        "occurred_at": occurred_at,
        "source": "tree",
        "agent_id": agent_id,
        "parent_agent_id": str(state.parent_id) if state.parent_id else None,
        "role": state.role,
        "depth": state.depth,
        "sequence_number": sequence_number_override,
    }

    if name == "RunStarted":
        return NormalizedEvent(
            **base,
            event_type="run_started",
            payload=RunLifecyclePayload(
                root_id=str(getattr(ev, "root_id", ev.aggregate_id)),
                status="started",
                duration_seconds=None,
            ),
        )

    if name == "RunCompleted":
        status = getattr(ev, "status", None) or "completed"
        return NormalizedEvent(
            **base,
            event_type="run_completed",
            payload=RunLifecyclePayload(
                root_id=str(getattr(ev, "root_id", ev.aggregate_id)),
                status=str(status),
                duration_seconds=_to_float_or_none(getattr(ev, "duration_seconds", None)),
            ),
        )

    if name == "AgentCreated":
        return NormalizedEvent(
            **base,
            event_type="agent_created",
            payload=AgentCreatedPayload(
                role=state.role,
                task_description=state.task_description,
                depth=state.depth,
            ),
        )

    if name == "PromptSent":
        op_raw = str(getattr(ev, "prompt_type", "worker_execution"))
        return NormalizedEvent(
            **base,
            event_type="prompt_sent",
            payload=PromptSentPayload(
                prompt_text=str(getattr(ev, "prompt", "")),
                prompt_type=_map_operation(op_raw),  # type: ignore[arg-type]
                model=str(getattr(ev, "target", "unknown")),
            ),
        )

    if name == "TokensConsumed":
        op_raw = str(getattr(ev, "operation", "worker_execution"))
        prompt_tokens = int(getattr(ev, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(ev, "completion_tokens", 0) or 0)
        # TokensConsumed's current schema doesn't split cache tokens; use what's there.
        return NormalizedEvent(
            **base,
            event_type="tokens_consumed",
            payload=TokensConsumedPayload(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                cache_read_input_tokens=int(getattr(ev, "cache_read_tokens", 0) or 0),
                cache_creation_input_tokens=int(getattr(ev, "cache_write_tokens", 0) or 0),
                thinking_tokens=_to_int_or_none(getattr(ev, "reasoning_tokens", None)),
                cost_usd=float(getattr(ev, "cost_usd", 0.0) or 0.0),
                operation=_map_operation(op_raw),  # type: ignore[arg-type]
                model=_to_str_or_none(getattr(ev, "model", None)),
            ),
        )

    if name == "WorkerCostRecorded":
        return NormalizedEvent(
            **base,
            event_type="worker_cost_recorded",
            payload=WorkerCostRecordedPayload(
                tool_name=str(getattr(ev, "tool_name", "unknown")),
                model=_to_str_or_none(getattr(ev, "model", None)),
                tokens=_to_int_or_none(getattr(ev, "tokens", None)),
                prompt_tokens=_to_int_or_none(getattr(ev, "prompt_tokens", None)),
                completion_tokens=_to_int_or_none(getattr(ev, "completion_tokens", None)),
                cache_read_tokens=_to_int_or_none(getattr(ev, "cache_read_tokens", None)),
                cache_write_tokens=_to_int_or_none(getattr(ev, "cache_write_tokens", None)),
                reasoning_tokens=_to_int_or_none(getattr(ev, "reasoning_tokens", None)),
                cost_usd=float(getattr(ev, "cost_usd", 0.0) or 0.0),
                duration_seconds=float(getattr(ev, "duration_seconds", 0.0) or 0.0),
            ),
        )

    if name == "ThoughtCaptured":
        output_type = str(getattr(ev, "output_type", "output"))
        content = str(getattr(ev, "content", ""))
        if output_type == "tool_use":
            tool_input_json = getattr(ev, "tool_input_json", None)
            raw_tool_name = getattr(ev, "tool_name", None)
            return NormalizedEvent(
                **base,
                event_type="tool_use",
                payload=ToolUsePayload(
                    call_id=_to_str_or_none(getattr(ev, "call_id", None)),
                    tool_name=raw_tool_name or "Unknown",
                    tool_input=tool_input_json if isinstance(tool_input_json, dict) else {},
                    duration_ms=_to_int_or_none(getattr(ev, "duration_ms", None)),
                ),
            )
        if output_type == "tool_result":
            return NormalizedEvent(
                **base,
                event_type="tool_result",
                payload=ToolResultPayload(
                    call_id=_to_str_or_none(getattr(ev, "call_id", None)),
                    result_text=content,
                    was_truncated=bool(getattr(ev, "was_truncated", False)),
                    result_bytes=_to_int_or_none(getattr(ev, "result_bytes", None)),
                    is_error=False,  # domain doesn't currently split out is_error
                ),
            )
        # thinking / progress / output text: emit as generic so CNR can still see them.
        return NormalizedEvent(
            **base,
            event_type="tool_use" if output_type == "output" else "tool_result",
            payload=GenericPayload(data={"output_type": output_type, "content": content}),
        )

    if name == "StatusChanged":
        return NormalizedEvent(
            **base,
            event_type="status_changed",
            payload=StatusChangedPayload(
                old_status=str(getattr(ev, "old_status", "")),
                new_status=str(getattr(ev, "new_status", "")),
            ),
        )

    if name == "SubtasksDefined":
        subtasks = getattr(ev, "subtasks", []) or []
        return NormalizedEvent(
            **base,
            event_type="subtasks_defined",
            payload=SubtasksDefinedPayload(subtask_count=len(subtasks)),
        )

    if name == "ChildSpawned":
        child_id = getattr(ev, "child_id", None)
        description = ""
        subtask = getattr(ev, "subtask", None)
        if subtask is not None:
            description = str(getattr(subtask, "description", "") or "")
        return NormalizedEvent(
            **base,
            event_type="child_spawned",
            payload=ChildSpawnedPayload(
                child_id=str(child_id) if child_id else "",
                description=description,
            ),
        )

    if name == "WorkCompleted":
        return NormalizedEvent(
            **base,
            event_type="work_completed",
            payload=GenericPayload(
                data={"result": str(getattr(ev, "result", ""))},
            ),
        )

    if name == "WorkFailed":
        return NormalizedEvent(
            **base,
            event_type="work_failed",
            payload=GenericPayload(data={"reason": str(getattr(ev, "reason", ""))}),
        )

    if name == "RetryScheduled":
        return NormalizedEvent(
            **base,
            event_type="retry_scheduled",
            payload=RetryScheduledPayload(
                attempt=int(getattr(ev, "attempt", 1) or 1),
                reason=str(getattr(ev, "reason", "")),
                escalated_model=_to_str_or_none(getattr(ev, "escalated_model", None)),
                is_verification_retry=bool(
                    getattr(ev, "is_verification_retry", False)
                ),
            ),
        )

    if name == "VerificationFailed":
        stages_passed = list(getattr(ev, "stages_passed", []) or [])
        return NormalizedEvent(
            **base,
            event_type="verification_failed",
            payload=VerificationFailedPayload(
                failed_stage=str(getattr(ev, "failed_stage", "unknown")),
                feedback=str(getattr(ev, "feedback", "")),
                stages_passed=stages_passed,
            ),
        )

    if name == "RedecompositionTriggered":
        return NormalizedEvent(
            **base,
            event_type="redecomposition_triggered",
            payload=GenericPayload(data={"reason": str(getattr(ev, "reason", ""))}),
        )

    # Everything else goes through as Generic so we don't lose signal silently.
    # Common unmodeled events include: ComplexityEvaluated (folded into role),
    # TaskAssigned (folded into agent state), CodeGenerationStarted, ProbeStarted/
    # Completed, OperationStarted/Finished, SharedContextCreated, ArtifactStored,
    # DecisionRecorded, ChildCompleted, ChildFailed.
    try:
        raw_data = ev.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        raw_data = {"__repr__": repr(ev)}
    return NormalizedEvent(
        **base,
        event_type="status_changed" if name == "TaskAssigned" else "tool_result",
        payload=GenericPayload(data={"domain_event_type": name, "fields": raw_data}),
    )



def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


# --------------------------------------------------------------------------- #
# CLI / orchestration
# --------------------------------------------------------------------------- #


def _read_last_run(run_dir: Path) -> dict[str, Any]:
    """Read ``.last_run.json`` written by RunPersistence."""
    path = run_dir / ".last_run.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Tree run must write .last_run.json via "
            "RunPersistence (enabled when --output-dir is passed to main.py)."
        )
    return json.loads(path.read_text())


def _read_existing_meta(run_dir: Path) -> dict[str, Any]:
    """Read a partially-populated meta.json (written by the runner) if present."""
    path = run_dir / "meta.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning("existing meta.json is not valid JSON; ignoring")
    return {}


def _collect_code_shas() -> dict[str, str]:
    shas: dict[str, str] = {}
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
        ).decode().strip()
        shas["arise_sec_lion"] = sha
    except Exception:  # noqa: BLE001
        shas["arise_sec_lion"] = "unknown"
    return shas


async def _project(run_dir: Path, config_path: Path | None) -> int:
    last_run = _read_last_run(run_dir)
    boss_id_str = last_run.get("boss_id") or last_run.get("root_id")
    if not boss_id_str:
        logger.error("no boss_id in .last_run.json; cannot project")
        return 2
    boss_id = UUID(boss_id_str)

    settings = Settings.from_yaml(config_path) if config_path else Settings.load()
    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        grouped = await store.get_hierarchy_events_grouped(boss_id)
    finally:
        await store.disconnect()

    states = _build_agent_index(grouped, boss_id)

    # Flatten to a single time-ordered stream.
    all_events: list[tuple[UUID, DomainEvent]] = []
    for agent_id, events in grouped.items():
        for ev in events:
            all_events.append((agent_id, ev))
    all_events.sort(
        key=lambda t: (_ensure_aware(t[1].occurred_at), int(t[1].sequence_number))
    )

    # Derive run_id from the existing meta (runner writes cve+cell+replicate) or
    # from the directory name pattern if the runner didn't write meta yet.
    existing_meta = _read_existing_meta(run_dir)
    run_id = existing_meta.get("run_id")
    if not run_id:
        # Directory layout is dataset/runs/<cve>/<cell>/<replicate>
        parts = run_dir.resolve().parts
        if len(parts) >= 3:
            run_id = f"{parts[-3]}-{parts[-2]}-{parts[-1]}"
        else:
            run_id = boss_id_str

    normalized: list[NormalizedEvent] = []
    emitted = 0
    global_seq = 0
    for agent_id, ev in all_events:
        state = states.get(agent_id)
        if state is None:
            logger.warning("no state for agent %s; skipping %s", agent_id, type(ev).__name__)
            continue
        try:
            norm = _convert_event(ev, state, run_id, global_seq)
        except Exception:  # noqa: BLE001
            logger.exception("failed to convert %s", type(ev).__name__)
            continue
        if norm is None:
            continue
        normalized.append(norm)
        emitted += 1
        global_seq += 1

    # Write events.jsonl
    events_path = run_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as fh:
        for n in normalized:
            fh.write(n.model_dump_json() + "\n")
    logger.info("wrote %d events to %s", emitted, events_path)

    # Update meta.json with fields derivable from the events.
    started_at, ended_at, wallclock = _lifecycle_from_events(normalized)
    termination_reason = _derive_termination(normalized, existing_meta)
    run_meta_kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cve_id": existing_meta.get("cve_id") or _parse_cve_from_run_dir(run_dir),
        "cell": existing_meta.get("cell") or _parse_cell_from_run_dir(run_dir),
        "replicate": existing_meta.get("replicate", _parse_replicate_from_run_dir(run_dir)),
        "system": "tree",
        "domain_briefing_enabled": bool(existing_meta.get("domain_briefing_enabled", False)),
        "subagent_enabled": existing_meta.get("subagent_enabled"),
        "prompt_strategy": existing_meta.get("prompt_strategy", "secbench"),
        "docker_image": existing_meta.get("docker_image", "unknown"),
        "budget_usd_cap": float(existing_meta.get("budget_usd_cap", 10.0)),
        "wallclock_sec_cap": int(existing_meta.get("wallclock_sec_cap", 600)),
        "models": existing_meta.get(
            "models",
            {
                "boss": settings.boss.model,
                "manager": settings.manager.model,
                "worker": settings.worker.model,
                "judge": existing_meta.get("judge_model", "gpt-5.4-pro"),
            },
        ),
        "started_at": started_at,
        "ended_at": ended_at,
        "wallclock_seconds": wallclock,
        "termination_reason": termination_reason,
        "code_sha": existing_meta.get("code_sha", _collect_code_shas()),
        "env": existing_meta.get("env", {"date": datetime.now(UTC).date().isoformat()}),
        "dataset_schema_version": SCHEMA_VERSION,
        "notes": existing_meta.get("notes", ""),
    }
    try:
        meta = RunMeta(**run_meta_kwargs)
        (run_dir / "meta.json").write_text(meta.model_dump_json(indent=2))
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to build RunMeta; leaving partial meta.json. "
            "kwargs=%r", {k: v for k, v in run_meta_kwargs.items() if k != "models"}
        )
        return 1

    return 0


def _lifecycle_from_events(
    events: list[NormalizedEvent],
) -> tuple[datetime, datetime, float]:
    started = None
    ended = None
    for ev in events:
        if ev.event_type == "run_started" and started is None:
            started = ev.occurred_at
        if ev.event_type == "run_completed":
            ended = ev.occurred_at
    if started is None:
        started = events[0].occurred_at if events else datetime.now(UTC)
    if ended is None:
        ended = events[-1].occurred_at if events else started
    wallclock = (ended - started).total_seconds()
    return _ensure_aware(started), _ensure_aware(ended), wallclock


def _derive_termination(
    events: list[NormalizedEvent],
    existing_meta: dict[str, Any],
) -> str:
    """Best-effort termination_reason from RunCompleted.status or meta override."""
    override = existing_meta.get("termination_reason")
    if override in (
        "completed",
        "budget_cap",
        "wallclock_cap",
        "llm_error",
        "container_error",
        "tree_timeout",
    ):
        return override
    for ev in reversed(events):
        if ev.event_type != "run_completed":
            continue
        status = getattr(ev.payload, "status", None) or "completed"
        status_lower = str(status).lower()
        if "timeout" in status_lower or "timed_out" in status_lower:
            return "tree_timeout"
        return "completed"
    return "completed"


_RUN_ID_PATTERN = re.compile(r"^(?P<cve>[^/]+)[-/](?P<cell>[AB][1234])[-/](?P<rep>\d+)$")


def _parse_cve_from_run_dir(run_dir: Path) -> str:
    parts = run_dir.resolve().parts
    return parts[-3] if len(parts) >= 3 else "unknown"


def _parse_cell_from_run_dir(run_dir: Path) -> str:
    parts = run_dir.resolve().parts
    candidate = parts[-2] if len(parts) >= 2 else "B1"
    if candidate in ("A1", "A2", "A3", "A4", "B1", "B2"):
        return candidate
    return "B1"


def _parse_replicate_from_run_dir(run_dir: Path) -> int:
    parts = run_dir.resolve().parts
    last = parts[-1] if parts else "0"
    try:
        return int(last)
    except ValueError:
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Tree run projection.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML config path (default: Settings.load() with ARISE_ENV)",
    )
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    if not run_dir.exists():
        logger.error("run-dir does not exist: %s", run_dir)
        return 2

    try:
        return asyncio.run(_project(run_dir, args.config))
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2
    except Exception:  # noqa: BLE001
        logger.exception("tree_projection failed unexpectedly")
        return 1


if __name__ == "__main__":
    sys.exit(main())
