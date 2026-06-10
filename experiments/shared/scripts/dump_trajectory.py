"""Dump a run's event trajectory to a flat, diff-friendly text file.

Every event becomes one logical line, in `sequence_number` order, grouped by
node. Tool-call events are normalized to ``TOOLCALL <tool_name> :: <content>``
(per the comparison spec); tool results, thoughts, prompts, probes, costs and
lifecycle markers get their own one-line renderings. Source of truth is the
Postgres ``events`` table only.

Usage::

    # N1: the single flat agent's whole trajectory
    uv run python -m experiments.shared.scripts.dump_trajectory \
        --study n1-openhands-linear --task faad2.cve-2018-20196 \
        --nodes all --out trajectory_n1.txt

    # B4: only the leaf worker nodes (agents that ran worker_execution)
    uv run python -m experiments.shared.scripts.dump_trajectory \
        --study b4-boss-manager-worker --task faad2.cve-2018-20196 \
        --nodes workers --out trajectory_b4_workers.txt

``--nodes``: ``all`` (every node), ``workers`` (only nodes with a
``worker_execution`` operation — the leaf executors), or a bracket role name
substring (e.g. ``PoC-Researcher``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import asyncpg

from experiments.shared.scripts._paths import get_repo_root


_BRACKET_RE = re.compile(r"\s*\[([A-Za-z-]+)\]")
_INPUT_RE = re.compile(r"Input:\s*(\{.*\})", re.DOTALL)
_MAX_CONTENT = 4000  # cap any single rendered field so one giant blob can't dominate the diff


def _resolve_run_ids(study: str, task: str | None, run_id: str | None, repo_root: Path) -> list[str]:
    if run_id:
        return [run_id]
    out: list[str] = []
    for mp in sorted((repo_root / "runs").glob("*/run_manifest.json")):
        try:
            d = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("study_id") != study:
            continue
        if task and d.get("task") != task:
            continue
        out.append(str(d.get("run_id") or mp.parent.name))
    return out


async def _connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "arise"),
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.getenv("POSTGRES_DB", "arise_events"),
    )
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    return conn


_TREE_SQL = """
WITH RECURSIVE tree(agg, depth, parent) AS (
    SELECT $1::uuid, 0, NULL::uuid
    UNION ALL
    SELECT e.aggregate_id, t.depth + 1, t.agg
    FROM events e JOIN tree t ON (e.payload->>'parent_id')::uuid = t.agg
    WHERE e.event_type = 'AgentCreated'
)
SELECT agg, depth, parent FROM tree
"""


@dataclass
class Node:
    agg: str
    depth: int
    role: str = "?"
    is_worker: bool = False
    events: list[asyncpg.Record] = field(default_factory=list)


async def _load_nodes(conn: asyncpg.Connection, root: str) -> dict[str, Node]:
    rows = await conn.fetch(_TREE_SQL, UUID(root))
    nodes = {str(r["agg"]): Node(agg=str(r["agg"]), depth=r["depth"]) for r in rows}
    aggs = [UUID(a) for a in nodes]
    nodes[root].role = "boss"
    for r in await conn.fetch(
        "SELECT aggregate_id, payload->>'task_description' td FROM events "
        "WHERE aggregate_id = ANY($1::uuid[]) AND event_type = 'TaskAssigned'",
        aggs,
    ):
        m = _BRACKET_RE.match(r["td"] or "")
        if m:
            nodes[str(r["aggregate_id"])].role = m.group(1)
    for r in await conn.fetch(
        "SELECT DISTINCT aggregate_id FROM events WHERE aggregate_id = ANY($1::uuid[]) "
        "AND event_type = 'OperationStarted' AND payload->>'operation_type' = 'worker_execution'",
        aggs,
    ):
        nodes[str(r["aggregate_id"])].is_worker = True
    for r in await conn.fetch(
        "SELECT aggregate_id, event_type, sequence_number, payload FROM events "
        "WHERE aggregate_id = ANY($1::uuid[]) ORDER BY aggregate_id, sequence_number",
        aggs,
    ):
        nodes[str(r["aggregate_id"])].events.append(r)
    return nodes


def _clip(text: str | None) -> str:
    if not text:
        return ""
    one = " ".join(text.split())
    return one if len(one) <= _MAX_CONTENT else one[:_MAX_CONTENT] + f" …[+{len(one) - _MAX_CONTENT} chars]"


def _normalize_toolcall(payload: dict) -> tuple[str, str]:
    """Return (tool_name, normalized content) for a tool_use ThoughtCaptured."""
    tool = payload.get("tool_name") or "unknown"
    content = payload.get("content") or ""
    m = _INPUT_RE.search(content)
    if m:
        return tool, _clip(m.group(1))
    # fall back to whatever follows the "Tool: X" header
    return tool, _clip(content.split("\n", 1)[-1] if "\n" in content else content)


def _render_event(rec: asyncpg.Record) -> str | None:
    et = rec["event_type"]
    p = rec["payload"]
    seq = rec["sequence_number"]
    if et == "ThoughtCaptured":
        ot = p.get("output_type")
        if ot == "tool_use":
            name, content = _normalize_toolcall(p)
            return f"[{seq}] TOOLCALL\t{name}\t{content}"
        if ot == "tool_result":
            return f"[{seq}] TOOLRESULT\t{_clip(p.get('content'))}"
        if ot == "thinking":
            return f"[{seq}] THINK\t{_clip(p.get('content'))}"
        return f"[{seq}] TEXT\t{_clip(p.get('content'))}"
    if et == "PromptSent":
        return f"[{seq}] PROMPT target={p.get('target')}\t{_clip(p.get('prompt'))}"
    if et == "ProbeStarted":
        return f"[{seq}] PROBE\t{p.get('probe_type')}"
    if et == "ProbeCompleted":
        return f"[{seq}] PROBE_RESULT\t{p.get('probe_type')}\t{_clip(p.get('result_summary'))}"
    if et in ("WorkCompleted",):
        return f"[{seq}] DONE\t{_clip(p.get('result'))}"
    if et in ("WorkFailed",):
        return f"[{seq}] FAILED\t{_clip(p.get('reason'))}"
    if et == "DecisionRecorded":
        return f"[{seq}] DECISION\t{_clip(p.get('decision') or p.get('content'))}"
    if et == "WorkerCostRecorded":
        return (
            f"[{seq}] COST model={p.get('model')} prompt={p.get('prompt_tokens')} "
            f"completion={p.get('completion_tokens')} cache_read={p.get('cache_read_tokens')} "
            f"cost=${p.get('cost_usd')}"
        )
    if et == "TokensConsumed":
        return (
            f"[{seq}] LLMUSAGE op={p.get('operation')} model={p.get('model')} "
            f"prompt={p.get('prompt_tokens')} completion={p.get('completion_tokens')} "
            f"cache_read={p.get('cache_read_tokens')} cost=${p.get('cost_usd')}"
        )
    if et == "TaskAssigned":
        return f"[{seq}] TASK\t{_clip(p.get('task_description'))}"
    if et in ("OperationStarted", "OperationFinished"):
        return f"[{seq}] {et.upper()[:9]}\t{p.get('operation_type')}"
    if et in ("RunStarted", "RunCompleted", "AgentCreated", "ChildSpawned",
              "AgentExecutionStarted", "AgentExecutionFinished", "CodeGenerationStarted"):
        return f"[{seq}] {et}"
    return None  # skip noise


def _select(nodes: dict[str, Node], which: str) -> list[Node]:
    ordered = sorted(nodes.values(), key=lambda n: (n.depth, n.role, n.agg))
    if which == "all":
        return ordered
    if which == "workers":
        return [n for n in ordered if n.is_worker]
    return [n for n in ordered if which.lower() in n.role.lower()]


def _dump(nodes: dict[str, Node], which: str, run_id: str, label: str) -> str:
    selected = _select(nodes, which)
    lines = [
        f"################ TRAJECTORY  study={label}  run={run_id}  nodes={which} "
        f"({len(selected)} of {len(nodes)}) ################",
    ]
    for n in selected:
        lines.append("")
        lines.append(f"#=== node {n.agg[:8]} role={n.role} depth={n.depth} "
                     f"worker={n.is_worker} events={len(n.events)} ===")
        for rec in n.events:
            rendered = _render_event(rec)
            if rendered is not None:
                lines.append(rendered)
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> str:
    repo_root = get_repo_root()
    run_ids = _resolve_run_ids(args.study, args.task, args.run_id, repo_root)
    if not run_ids:
        raise SystemExit(f"no run found for study={args.study} task={args.task} run_id={args.run_id}")
    conn = await _connect()
    chunks: list[str] = []
    try:
        for run_id in run_ids:
            nodes = await _load_nodes(conn, run_id)
            chunks.append(_dump(nodes, args.nodes, run_id, args.study))
    finally:
        await conn.close()
    return "\n\n".join(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(prog="dump_trajectory", description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--task", default=None, help="CVE task slug; omit to dump every run of the study")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--nodes", default="all", help="all | workers | <role-substring>")
    parser.add_argument("--out", default=None, help="output file (default stdout)")
    args = parser.parse_args()

    text = asyncio.run(_run(args))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text)} chars, {text.count(chr(10))} lines)")
    else:
        print(text)


if __name__ == "__main__":
    main()
