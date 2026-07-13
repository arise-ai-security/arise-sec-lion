#!/usr/bin/env python3
"""List workers in a run, ordered by creation time.

For a given BOSS run_id, traverses the hierarchy and emits one line per
descendant agent with its worker_id and the bracketed role name parsed
from its task description (e.g., ``[Analysis] ...`` -> ``Analysis``).

Usage:
    POSTGRES_PASSWORD=arise python scripts/list_run_workers.py <run_id>
    POSTGRES_PASSWORD=arise python scripts/list_run_workers.py <run_id> --include-boss
    POSTGRES_PASSWORD=arise python scripts/list_run_workers.py <run_id> --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID


_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.settings import ApiSettings  # noqa: E402
from core.domain.events.events import AgentCreated, DomainEvent, TaskAssigned  # noqa: E402
from infrastructure.adapters.postgres_event_store import PostgresEventStore  # noqa: E402


if TYPE_CHECKING:
    from datetime import datetime


_ROLE_PREFIX_RE = re.compile(r"\[([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class WorkerRow:
    worker_id: UUID
    role_name: str
    agent_role: str
    created_at: datetime
    sibling_index: int
    task_preview: str


def _extract_role_name(task_description: str | None) -> str:
    if not task_description:
        return ""
    match = _ROLE_PREFIX_RE.match(task_description.strip())
    return match.group(1).strip() if match else ""


def _find_first(events: list[DomainEvent], cls: type[DomainEvent]) -> DomainEvent | None:
    for event in events:
        if isinstance(event, cls):
            return event
    return None


def _build_rows(
    grouped: dict[UUID, list[DomainEvent]],
    run_id: UUID,
    include_boss: bool,
) -> list[WorkerRow]:
    rows: list[WorkerRow] = []
    for aggregate_id, events in grouped.items():
        created = _find_first(events, AgentCreated)
        if not isinstance(created, AgentCreated):
            continue
        if not include_boss and aggregate_id == run_id:
            continue

        assigned = _find_first(events, TaskAssigned)
        task_description = (
            assigned.task_description if isinstance(assigned, TaskAssigned) else ""
        )
        rows.append(
            WorkerRow(
                worker_id=aggregate_id,
                role_name=_extract_role_name(task_description),
                agent_role=created.role,
                created_at=created.occurred_at,
                sibling_index=created.sibling_index,
                task_preview=task_description.strip().splitlines()[0][:120]
                if task_description
                else "",
            )
        )

    rows.sort(key=lambda r: (r.created_at, r.sibling_index, str(r.worker_id)))
    return rows


def _format_table(rows: list[WorkerRow]) -> str:
    if not rows:
        return "(no agents found for this run)"

    headers = ("#", "worker_id", "role_name", "agent_role", "created_at", "task_preview")
    table = [headers]
    for idx, row in enumerate(rows, start=1):
        table.append(
            (
                str(idx),
                str(row.worker_id),
                row.role_name or "-",
                row.agent_role,
                row.created_at.isoformat(timespec="seconds"),
                row.task_preview or "-",
            )
        )

    widths = [max(len(cell) for cell in column) for column in zip(*table, strict=True)]
    lines = []
    for r_idx, cells in enumerate(table):
        line = "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(cells))
        lines.append(line)
        if r_idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _format_json(rows: list[WorkerRow]) -> str:
    return json.dumps(
        [
            {
                "worker_id": str(row.worker_id),
                "role_name": row.role_name,
                "agent_role": row.agent_role,
                "created_at": row.created_at.isoformat(),
                "task_preview": row.task_preview,
            }
            for row in rows
        ],
        indent=2,
    )


async def _run(run_id: UUID, *, include_boss: bool, as_json: bool) -> int:
    settings = ApiSettings.load()
    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        grouped = await store.get_hierarchy_events_grouped(run_id)
    finally:
        await store.disconnect()

    rows = _build_rows(grouped, run_id, include_boss=include_boss)
    print(_format_json(rows) if as_json else _format_table(rows))
    return 0 if rows else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_id", help="UUID of the BOSS run (root agent)")
    parser.add_argument(
        "--include-boss",
        action="store_true",
        help="Include the BOSS row (run_id itself) in the output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text table",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        run_id = UUID(args.run_id)
    except ValueError:
        print(f"error: invalid UUID for run_id: {args.run_id!r}", file=sys.stderr)
        return 2

    return asyncio.run(_run(run_id, include_boss=args.include_boss, as_json=args.json))


if __name__ == "__main__":
    sys.exit(main())
