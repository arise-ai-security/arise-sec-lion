"""CLI: `python -m experiments.shared.scripts.db ...`.

Subcommands::

    run <run_id>      [--pretty]                   # full subtree
    agent <agent_id>  [--pretty] [--no-parent-outcome]
    lineage <node_id> [--pretty]                   # boss → ... → node
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

import orjson

from experiments.shared.scripts.db import (
    EventRow,
    fetch_run_events,
    get_agent_trajectory,
    get_parent_trace,
    open_connection,
    prettify_trajectory,
)


def _row_to_dict(e: EventRow) -> dict:
    return {
        "event_id": str(e.event_id),
        "aggregate_id": str(e.aggregate_id),
        "sequence_number": e.sequence_number,
        "event_type": e.event_type,
        "payload": e.payload,
        "occurred_at": e.occurred_at.isoformat(),
        "metadata": e.metadata,
    }


def _emit_jsonl(events: list[EventRow]) -> None:
    for e in events:
        sys.stdout.buffer.write(orjson.dumps(_row_to_dict(e)))
        sys.stdout.buffer.write(b"\n")


async def _cmd_run(run_id: UUID, pretty: bool) -> int:
    conn = await open_connection()
    try:
        events = await fetch_run_events(conn, run_id)
    finally:
        await conn.close()
    if pretty:
        for line in prettify_trajectory(events):
            print(line)
    else:
        _emit_jsonl(events)
    return 0


async def _cmd_agent(
    agent_id: UUID, pretty: bool, with_parent_outcome: bool
) -> int:
    conn = await open_connection()
    try:
        result = await get_agent_trajectory(
            conn,
            agent_id,
            with_parent_outcome=with_parent_outcome,
            pretty=pretty,
        )
    finally:
        await conn.close()
    if pretty:
        for line in result:  # type: ignore[union-attr]
            print(line)
    else:
        _emit_jsonl(result)  # type: ignore[arg-type]
    return 0


async def _cmd_lineage(node_id: UUID, pretty: bool) -> int:
    conn = await open_connection()
    try:
        result = await get_parent_trace(conn, node_id, pretty=pretty)
    finally:
        await conn.close()
    if pretty:
        for line in result:  # type: ignore[union-attr]
            print(line)
    else:
        _emit_jsonl(result)  # type: ignore[arg-type]
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.shared.scripts.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="full subtree from boss / run_id")
    p_run.add_argument("run_id", type=UUID)
    p_run.add_argument("--pretty", action="store_true")

    p_agent = sub.add_parser("agent", help="trajectory for one agent")
    p_agent.add_argument("agent_id", type=UUID)
    p_agent.add_argument("--pretty", action="store_true")
    p_agent.add_argument(
        "--no-parent-outcome",
        dest="with_parent_outcome",
        action="store_false",
        help="omit the parent's ChildCompleted/ChildFailed tail event",
    )
    p_agent.set_defaults(with_parent_outcome=True)

    p_lineage = sub.add_parser("lineage", help="boss → ... → node ancestor trace")
    p_lineage.add_argument("node_id", type=UUID)
    p_lineage.add_argument("--pretty", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        return asyncio.run(_cmd_run(args.run_id, args.pretty))
    if args.cmd == "agent":
        return asyncio.run(
            _cmd_agent(args.agent_id, args.pretty, args.with_parent_outcome)
        )
    if args.cmd == "lineage":
        return asyncio.run(_cmd_lineage(args.node_id, args.pretty))
    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
