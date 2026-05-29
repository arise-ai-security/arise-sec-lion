"""Load and de-duplicate study run events from the canonical event store.

Source of truth is the Postgres ``events`` table (per the study mandate).
``runs/<id>/events.jsonl`` is the same hierarchy data projected by the
production pipeline; it is used here only as an offline cross-check, because
it does NOT contain the shared-store ("global context") aggregate — that
lives on the derived ``uuid5`` aggregate (`shared_context_aggregate_id`) and
must be fetched from the DB separately.

Three data-correctness guarantees live here (correctness requirement #1):
  1. membership   — only runs whose manifest ``study_id`` matches are kept;
  2. triangulation — directory name == manifest ``run_id`` == events root;
  3. de-duplication — exactly one run per CVE instance (``task``), with a
     full audit of what was discarded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from experiments.shared.scripts.db.models import EventRow


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import asyncpg


STUDY_ID = "b1-batch-autogen"

# exit_status preference when one CVE has several runs: prefer a successful
# run (complete tree), then a timeout (ran to the wall-clock limit, usually a
# fuller tree than an early crash), then a hard failure. Ties broken by the
# most recent ``started_at``.
_EXIT_RANK = {"success": 0, "timeout": 1, "failed": 2}

# Top-level columns of a DB event row. Every OTHER key in an events.jsonl
# line is a flattened payload field (the on-disk projection dumps the event
# model flat and stamps ``event_type``), so payload = line minus these keys.
_META_KEYS = frozenset(
    {"event_id", "aggregate_id", "sequence_number", "event_type", "occurred_at", "metadata"}
)


class RunIdMismatch(Exception):
    """Directory name disagrees with the manifest ``run_id`` (triangulation #2)."""


class RootMismatch(Exception):
    """The loaded events' root aggregate is not the expected ``run_id`` (#3)."""


@dataclass(frozen=True)
class RunInfo:
    """One enrolled run, sourced from ``runs/<id>/run_manifest.json``."""

    run_id: str
    task: str  # CVE instance id, e.g. "mruby.cve-2018-11743"
    exit_status: str
    started_at: str
    run_dir: Path

    @property
    def events_jsonl(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def has_events(self) -> bool:
        return self.events_jsonl.is_file()


@dataclass(frozen=True)
class DedupResult:
    """Outcome of one-run-per-CVE de-duplication, kept fully auditable."""

    chosen: list[RunInfo]
    discarded: dict[str, list[str]]  # task -> discarded run_ids
    multi_success: list[str]  # tasks with >1 success run (anomaly to surface)


def enumerate_runs(runs_root: str | Path, study_id: str = STUDY_ID) -> list[RunInfo]:
    """All runs under ``runs_root`` whose manifest ``study_id`` matches.

    Enforces triangulation leg #2 (dir name == manifest ``run_id``); raises
    :class:`RunIdMismatch` on the first disagreement (fail-fast).
    """
    runs_root = Path(runs_root)
    out: list[RunInfo] = []
    for manifest in sorted(runs_root.glob("*/run_manifest.json")):
        data = json.loads(manifest.read_text())
        if data.get("study_id") != study_id:
            continue
        dir_id = manifest.parent.name
        manifest_id = data.get("run_id")
        if manifest_id != dir_id:
            raise RunIdMismatch(f"dir {dir_id!r} != manifest run_id {manifest_id!r}")
        out.append(
            RunInfo(
                run_id=dir_id,
                task=data.get("task", ""),
                exit_status=data.get("exit_status", ""),
                started_at=data.get("started_at", ""),
                run_dir=manifest.parent,
            )
        )
    return out


def dedupe_by_cve(runs: Iterable[RunInfo]) -> DedupResult:
    """Collapse multiple runs of the same CVE ``task`` to a single winner.

    Winner = best ``_EXIT_RANK`` then latest ``started_at`` (ISO-8601 strings
    sort chronologically). Records discarded run_ids per task and flags any
    CVE that has more than one successful run (would mean success is not a
    unique representative).
    """
    by_task: dict[str, list[RunInfo]] = {}
    for run in runs:
        by_task.setdefault(run.task, []).append(run)

    chosen: list[RunInfo] = []
    discarded: dict[str, list[str]] = {}
    multi_success: list[str] = []
    for task, group in by_task.items():
        if sum(1 for r in group if r.exit_status == "success") > 1:
            multi_success.append(task)
        best_rank = min(_EXIT_RANK.get(r.exit_status, 9) for r in group)
        candidates = [r for r in group if _EXIT_RANK.get(r.exit_status, 9) == best_rank]
        winner = max(candidates, key=lambda r: r.started_at)
        chosen.append(winner)
        rest = sorted(r.run_id for r in group if r.run_id != winner.run_id)
        if rest:
            discarded[task] = rest

    chosen.sort(key=lambda r: r.task)
    return DedupResult(chosen=chosen, discarded=discarded, multi_success=sorted(multi_success))


def _row_from_flat(obj: dict) -> EventRow:
    """Reshape one flat events.jsonl object into the DB-shaped :class:`EventRow`.

    The DB keeps payload in its own JSONB column; the jsonl flattens those
    fields to the top level. Pulling out the six meta keys reconstructs the
    payload dict. Note: jsonl scalars stay strings (UUIDs, timestamps) whereas
    the DB returns typed objects — callers comparing the two must normalize.
    """
    payload = {k: v for k, v in obj.items() if k not in _META_KEYS}
    return EventRow(
        event_id=obj.get("event_id"),
        aggregate_id=obj.get("aggregate_id"),
        sequence_number=obj.get("sequence_number"),
        event_type=obj.get("event_type"),
        payload=payload,
        occurred_at=obj.get("occurred_at"),
        metadata=obj.get("metadata") or {},
    )


def load_events_jsonl(run_dir: str | Path) -> list[EventRow]:
    """Hierarchy events from ``runs/<id>/events.jsonl`` (offline cross-check)."""
    rows: list[EventRow] = []
    with (Path(run_dir) / "events.jsonl").open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(_row_from_flat(json.loads(line)))
    return rows


def find_root_aggregate(events: Sequence[EventRow]) -> str | None:
    """The boss/root aggregate id implied by the events (triangulation #3).

    ``RunStarted`` is stored against the boss root; fall back to the lone
    ``AgentCreated`` with no ``parent_id``.
    """
    for event in events:
        if event.event_type == "RunStarted":
            return str(event.aggregate_id)
    for event in events:
        if event.event_type == "AgentCreated" and not event.payload.get("parent_id"):
            return str(event.aggregate_id)
    return None


def assert_root_matches(run_id: str, events: Sequence[EventRow]) -> None:
    """Triangulation leg #3: the loaded events really belong to ``run_id``."""
    root = find_root_aggregate(events)
    if root is not None and root != run_id:
        raise RootMismatch(f"events root {root!r} != run_id {run_id!r}")


async def open_db() -> asyncpg.Connection:
    """Open the canonical event-store connection.

    Reads ``POSTGRES_*`` env vars; the local-dev password falls back to
    ``"arise"`` (matching ``temp/export_b1.py`` / ``config/config.yaml``).
    """
    import os

    from experiments.shared.scripts.db.event_queries import open_connection

    return await open_connection(password=os.environ.get("POSTGRES_PASSWORD") or "arise")


async def load_events_db(conn: asyncpg.Connection, run_id: str | UUID) -> list[EventRow]:
    """All subtree events for ``run_id`` from the DB (time-ordered)."""
    from experiments.shared.scripts.db.event_queries import fetch_run_events

    return await fetch_run_events(conn, run_id if isinstance(run_id, UUID) else UUID(run_id))


async def load_shared_store_db(conn: asyncpg.Connection, run_id: str | UUID) -> list[EventRow]:
    """Shared-store ("global context") events for ``run_id`` (DB-only).

    These live on the derived ``uuid5`` aggregate, never in the subtree walk
    and never in ``events.jsonl``.
    """
    from core.domain.shared_context import shared_context_aggregate_id
    from experiments.shared.scripts.db.event_queries import fetch_agent_events

    rid = run_id if isinstance(run_id, UUID) else UUID(run_id)
    return await fetch_agent_events(conn, shared_context_aggregate_id(rid))
