"""Dump MANAGER-node events (full payloads) to a flat text file per run.

Manager = a depth-1 node whose operation resolves to decomposition. Depth-1 leaf
workers are excluded because they belong to the worker dump. Source of truth is
the Postgres ``events``
table only; every event is rendered as pretty JSON in (aggregate, sequence)
order, grouped by node, mirroring the worker-dump format.

Usage::

    uv run python scripts/dump_manager_events.py \
        --run f8508971-7b6e-4862-9d95-36d1938e0539 --task example-task \
        --out manager_events_f8508971.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

import asyncpg

_TREE_SQL = """
WITH RECURSIVE tree(agg, depth, parent) AS (
    SELECT e.aggregate_id, 0, NULL::uuid
    FROM events e
    WHERE e.event_type = 'RunStarted' AND e.aggregate_id = $1
    UNION ALL
    SELECT c.aggregate_id, t.depth + 1, t.agg
    FROM events c JOIN tree t ON (c.payload->>'parent_id')::uuid = t.agg
    WHERE c.event_type = 'AgentCreated'
)
SELECT agg::text AS agg, depth, parent::text AS parent FROM tree
"""

_ROLE_RE = re.compile(r"^\s*\[([A-Za-z0-9 _-]+)\]")


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "arise"),
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "arise_events"),
    )


async def _manager_aggs(conn: asyncpg.Connection, run: str) -> list[tuple[str, str]]:
    """Return (agg, branch_label) for depth-1 nodes whose operation is decomposition."""
    rows = await conn.fetch(_TREE_SQL, run)
    depth1 = [r["agg"] for r in rows if r["depth"] == 1]
    managers: list[tuple[str, str]] = []
    for agg in depth1:
        # A manager runs a decomposition operation and emits SubtasksDefined;
        # a leaf worker runs worker_execution instead.
        is_manager = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM events WHERE aggregate_id=$1::uuid "
            "AND event_type='SubtasksDefined')",
            agg,
        )
        if not is_manager:
            continue
        task = await conn.fetchval(
            "SELECT payload->>'task_description' FROM events "
            "WHERE aggregate_id=$1::uuid AND event_type='TaskAssigned' "
            "ORDER BY sequence_number LIMIT 1",
            agg,
        )
        m = _ROLE_RE.match(task or "")
        managers.append((agg, m.group(1) if m else "?"))
    return managers


async def _events(conn: asyncpg.Connection, agg: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT sequence_number, event_type, occurred_at, payload "
        "FROM events WHERE aggregate_id=$1::uuid ORDER BY sequence_number",
        agg,
    )


async def _run(args: argparse.Namespace) -> str:
    conn = await _connect()
    try:
        managers = await _manager_aggs(conn, args.run)
        blocks: list[list[asyncpg.Record]] = []
        total = 0
        for agg, _ in managers:
            evs = await _events(conn, agg)
            blocks.append(evs)
            total += len(evs)

        out: list[str] = []
        bar = "=" * 80
        out.append(bar)
        out.append("MANAGER-NODE EVENTS  (depth-1 decomposers only; boss + workers excluded)")
        out.append(f"Run:            {args.run}")
        out.append(f"Task:           {args.task}")
        out.append(f"Manager nodes:  {len(managers)}")
        out.append(f"Manager events: {total}")
        out.append("-" * 80)
        out.append("Manager aggregates (in tree order):")
        for i, (agg, branch) in enumerate(managers, 1):
            out.append(f"  {i:>2}. {agg}  role=manager depth=1 branch={branch} events={len(blocks[i-1])}")
        out.append(bar)
        out.append("")

        for i, ((agg, branch), evs) in enumerate(zip(managers, blocks), 1):
            out.append("#" * 70)
            out.append(f"MANAGER {i}/{len(managers)}  agg={agg}  role=manager  depth=1  branch={branch}  events={len(evs)}")
            out.append("#" * 70)
            out.append("")
            for rec in evs:
                payload = rec["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                ts = rec["occurred_at"].isoformat().replace("+00:00", "Z")
                out.append(f"----- [seq {rec['sequence_number']}] {rec['event_type']} @ {ts} -----")
                out.append(json.dumps(payload, indent=1, sort_keys=True, default=str))
                out.append("")
            out.append("")
        text = "\n".join(out)
    finally:
        await conn.close()

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        return f"wrote {args.out} ({len(text)} bytes, {total} events, {len(managers)} managers)"
    return text


def main() -> None:
    parser = argparse.ArgumentParser(prog="dump_manager_events", description=__doc__)
    parser.add_argument("--run", required=True, help="root/boss aggregate_id of the run")
    parser.add_argument("--task", default="", help="task label for the header")
    parser.add_argument("--out", default=None, help="output file (default stdout)")
    print(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
