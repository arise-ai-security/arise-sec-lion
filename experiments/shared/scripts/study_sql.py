"""Grounded SQL metrics for cost-family studies (N1/N2/B3/B4 and friends).

Single source of truth: the Postgres ``events`` table plus ``runs/*/run_manifest.json``
(study/cell/task attribution). No log parsing, no estimates — every number is an
aggregation over recorded events.

Subcommands::

    uv run python -m experiments.shared.scripts.study_sql runs       --studies a,b
    uv run python -m experiments.shared.scripts.study_sql cost       --studies a,b
    uv run python -m experiments.shared.scripts.study_sql cache      --studies a,b
    uv run python -m experiments.shared.scripts.study_sql tools      --studies a,b
    uv run python -m experiments.shared.scripts.study_sql files      --studies a,b
    uv run python -m experiments.shared.scripts.study_sql check      --studies a,b
    uv run python -m experiments.shared.scripts.study_sql invariants --studies a,b

* ``runs``  — per (study, cell, task): run id, exit status, agent count, wallclock.
* ``cost``  — tokens by class per (study, cell, model) + raw USD recomputed from
  provider price sheets + USD normalized to the reference model's rates.
* ``cache`` — cache-hit rate by (study, cell, role, depth):
  cache_read / (uncached_input + cache_read + cache_write).
* ``tools`` — tool-call counts by (study, cell, role, depth), split by tool name.
* ``files`` — redundant reads: files read by more than one agent within a run,
  and within a BEF subtree (branch label from the boss child's [Bracket] prefix).
* ``check`` — recomputed raw USD vs provider-reported cost_usd per model
  (validates the provider billing-semantics assumptions below).
* ``invariants`` — per-run design-invariant checks from the events alone: flat
  prompts free of tree/judge vocabulary, no judge-stage LLM usage, no opus
  models, one shared container per run, one fresh conversation + exactly one
  WorkerCostRecorded per worker. Exits 1 on any FAIL.

Token-usage semantics encoded here (validated by ``check`` at 0.0 drift): every
usage stream in this codebase reports ``prompt_tokens`` as the TOTAL input,
inclusive of cache tokens — LiteLLM and the OpenHands SDK normalize Anthropic
usage to OpenAI-style totals — so ``uncached = prompt - cache_read - cache_write``
for all rows. Per-provider billing applied to those buckets:

* OpenAI (``gpt*``): in*uncached + 0.1*in*cache_read + out*completion (no write
  surcharge).
* Anthropic (``claude*``): in*uncached + 0.1*in*cache_read + 1.25*in*cache_write
  + out*completion.

A row where ``prompt < cache_read + cache_write`` would mean a stream with
native-Anthropic (cache-exclusive) semantics leaked in; ``_usage_costs`` warns on
stderr when it sees one instead of silently clamping.

Cost metrics, most to least comparison-fair:

* ``normalized_usd`` — HEADLINE metric for cross-cell hypotheses: re-prices every
  call (all tiers, all models) at the REFERENCE model's rates while preserving
  the provider's cache-discount structure, isolating architecture+caching
  efficiency from vendor pricing:

      normalized = REF_IN*(uncached + 0.1*cache_read + write_ratio*cache_write)
                 + REF_OUT*completion

* ``prov_norm_usd`` — secondary: real billed dollars with only the Anthropic
  tier discounted by the claude/codex input-price ratio. NOTE: it does NOT
  normalize the worker-model gap (gpt-5.3-codex vs gpt-5.4-mini), so N-vs-B
  orderings can flip between this and ``normalized_usd``.
* ``reported_usd`` / ``raw_usd`` — provider-billed dollars (and their recompute).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from experiments.shared.scripts._paths import get_repo_root


# USD per token, verified 2026-06-09 (OpenAI pricing pages + Anthropic docs;
# matches litellm.model_cost for all three).
@dataclass(frozen=True)
class ModelPrice:
    input_per_tok: float
    output_per_tok: float
    cache_read_per_tok: float
    cache_write_per_tok: float
    prompt_includes_cached: bool  # OpenAI-style usage reporting


PRICES: dict[str, ModelPrice] = {
    "gpt-5.3-codex": ModelPrice(1.75e-6, 14.0e-6, 0.175e-6, 0.0, True),
    "gpt-5.4-mini": ModelPrice(0.75e-6, 4.5e-6, 0.075e-6, 0.0, True),
    "claude-sonnet-4-6": ModelPrice(3.0e-6, 15.0e-6, 0.30e-6, 3.75e-6, False),
    "claude-sonnet-4-5-20250929": ModelPrice(3.0e-6, 15.0e-6, 0.30e-6, 3.75e-6, False),
    "claude-opus-4-5-20251101": ModelPrice(5.0e-6, 25.0e-6, 0.50e-6, 6.25e-6, False),
}
REFERENCE_MODEL = "claude-sonnet-4-6"
CACHE_READ_RATIO = 0.1
ANTHROPIC_WRITE_RATIO = 1.25

# Provider-neutral normalization (user-defined): keep real billed dollars, but
# discount the Anthropic orchestration tier by the claude÷codex input-price
# ratio so B-cells aren't penalized purely for sonnet costing more per token
# than the gpt baseline. prov_norm_usd = Σ gpt cost + Σ anthropic cost / R.
PROVIDER_NORM_RATIO = 3.00 / 1.75  # claude-sonnet-4-6 input ÷ gpt-5.3-codex input = 1.714


def _is_anthropic(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return "claude" in m or "sonnet" in m or "opus" in m or "haiku" in m or m.startswith("anthropic")


def _price_for(model: str | None) -> ModelPrice | None:
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    bare = model.split("/", 1)[-1]
    return PRICES.get(bare)


def _is_openai_style(model: str) -> bool:
    price = _price_for(model)
    if price is not None:
        return price.prompt_includes_cached
    return model.split("/", 1)[-1].startswith(("gpt", "o1", "o3", "o4"))


@dataclass(frozen=True)
class RunRef:
    study: str
    cell: str
    task: str
    run_id: str
    exit_status: str | None


def load_runs_for_studies(
    studies: set[str], repo_root: Path, latest_per_cell_task: int | None = None
) -> list[RunRef]:
    """Map study/cell/task -> run ids from runs/*/run_manifest.json (allowed ground truth).

    ``latest_per_cell_task``: keep only the N most-recent runs (by manifest
    mtime) for each (study, cell, task). Re-running a study mints fresh run dirs
    under the same study_id, so this isolates one cycle's runs from prior ones.
    """
    scored: list[tuple[float, RunRef]] = []
    for manifest_path in sorted((repo_root / "runs").glob("*/run_manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("study_id") not in studies:
            continue
        run_id = data.get("run_id") or manifest_path.parent.name
        scored.append((
            manifest_path.stat().st_mtime,
            RunRef(
                study=str(data.get("study_id")),
                cell=str(data.get("cell") or "?"),
                task=str(data.get("task") or "?"),
                run_id=str(run_id),
                exit_status=data.get("exit_status"),
            ),
        ))
    if latest_per_cell_task is None:
        return [r for _, r in scored]
    by_key: dict[tuple[str, str, str], list[tuple[float, RunRef]]] = defaultdict(list)
    for mtime, ref in scored:
        by_key[(ref.study, ref.cell, ref.task)].append((mtime, ref))
    kept: list[RunRef] = []
    for items in by_key.values():
        items.sort(key=lambda t: t[0], reverse=True)
        kept.extend(r for _, r in items[:latest_per_cell_task])
    return kept


async def _connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "arise"),
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.getenv("POSTGRES_DB", "arise_events"),
    )
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    return conn


# Recursive agent tree per run root; depth 0 = boss. Roles come from the last
# ExecutionStarted (the executed role) and fall back to AgentCreated.role.
_TREE_SQL = """
WITH RECURSIVE tree(agg, depth, parent) AS (
    SELECT $1::uuid, 0, NULL::uuid
    UNION ALL
    SELECT e.aggregate_id, t.depth + 1, t.agg
    FROM events e
    JOIN tree t ON (e.payload->>'parent_id')::uuid = t.agg
    WHERE e.event_type = 'AgentCreated'
)
SELECT agg, depth, parent FROM tree
"""

_ROLES_SQL = """
SELECT DISTINCT ON (aggregate_id)
    aggregate_id,
    COALESCE(
        (SELECT e2.payload->>'role' FROM events e2
         WHERE e2.aggregate_id = e.aggregate_id AND e2.event_type = 'ExecutionStarted'
         ORDER BY e2.sequence_number DESC LIMIT 1),
        e.payload->>'role'
    ) AS role
FROM events e
WHERE e.aggregate_id = ANY($1::uuid[]) AND e.event_type = 'AgentCreated'
ORDER BY aggregate_id, sequence_number
"""

_TASKS_SQL = """
SELECT DISTINCT ON (aggregate_id) aggregate_id, payload->>'description' AS description
FROM events
WHERE aggregate_id = ANY($1::uuid[]) AND event_type = 'TaskAssigned'
ORDER BY aggregate_id, sequence_number
"""

_USAGE_SQL = """
SELECT aggregate_id, event_type,
       payload->>'model' AS model,
       payload->>'operation' AS operation,
       COALESCE((payload->>'prompt_tokens')::bigint, 0) AS prompt_tokens,
       COALESCE((payload->>'completion_tokens')::bigint, 0) AS completion_tokens,
       COALESCE((payload->>'cache_read_tokens')::bigint, 0) AS cache_read_tokens,
       COALESCE((payload->>'cache_write_tokens')::bigint, 0) AS cache_write_tokens,
       COALESCE((payload->>'cost_usd')::float, 0) AS cost_usd
FROM events
WHERE aggregate_id = ANY($1::uuid[])
  AND event_type IN ('TokensConsumed', 'WorkerCostRecorded')
"""

_TOOLCALL_SQL = """
SELECT aggregate_id,
       CASE WHEN event_type = 'ProbeStarted'
            THEN COALESCE(payload->>'tool_name', payload->>'tool', 'recon')
            ELSE COALESCE(payload->>'tool_name', 'unknown')
       END AS tool_name,
       CASE WHEN event_type = 'ProbeStarted'
            THEN payload::text
            ELSE payload->>'content'
       END AS detail,
       event_type
FROM events
WHERE aggregate_id = ANY($1::uuid[])
  AND (
        (event_type = 'ThoughtCaptured' AND payload->>'output_type' = 'tool_use')
     OR event_type = 'ProbeStarted'
  )
"""

_RUN_WALL_SQL = """
SELECT aggregate_id,
       COALESCE((payload->>'duration_seconds')::float, 0) AS duration_seconds,
       payload->>'status' AS status
FROM events
WHERE aggregate_id = ANY($1::uuid[]) AND event_type = 'RunCompleted'
"""

_BRACKET_RE = re.compile(r"\[([A-Za-z-]+)\]")
_PATH_KEYS = ("path", "file_path", "target_file", "filename", "file")
_SEMANTICS_WARNED: set[str] = set()

# Tree/judge vocabulary that must never appear in a flat (N-cell) prompt. These
# are template-specific phrases, not generic words, so CVE descriptions cannot
# trip them. The N2 native-subagent note says "delegate", not "decompose".
FLAT_PROMPT_BLACKLIST = (
    "decompos",
    "subtask",
    "sibling",
    "peer worker",
    "verification judge",
    "provided_source_files",
    "your manager",
    "<context-update>",
)
# Anchors every flat prompt must contain (BEF pipeline + task input).
FLAT_PROMPT_REQUIRED = ("<pipeline>", "<task>")


@dataclass
class AgentNode:
    agg: str
    depth: int
    parent: str | None
    role: str = "?"
    branch: str = "-"


async def _load_tree(conn: asyncpg.Connection, root: str) -> dict[str, AgentNode]:
    rows = await conn.fetch(_TREE_SQL, UUID(root))
    nodes = {
        str(r["agg"]): AgentNode(
            agg=str(r["agg"]),
            depth=r["depth"],
            parent=str(r["parent"]) if r["parent"] else None,
        )
        for r in rows
    }
    aggs = [UUID(a) for a in nodes]
    for r in await conn.fetch(_ROLES_SQL, aggs):
        nodes[str(r["aggregate_id"])].role = r["role"] or "?"
    nodes[root].role = "boss"
    # Managers never emit ExecutionStarted, so they keep the AgentCreated
    # 'pending' role; resolve any non-leaf still labeled pending as manager.
    parents = {node.parent for node in nodes.values() if node.parent}
    for node in nodes.values():
        if node.agg != root and node.role in ("pending", "?") and node.agg in parents:
            node.role = "manager"

    branch_of: dict[str, str] = {}
    for r in await conn.fetch(_TASKS_SQL, aggs):
        agg = str(r["aggregate_id"])
        node = nodes[agg]
        if node.parent == root:
            match = _BRACKET_RE.match((r["description"] or "").strip())
            branch_of[agg] = match.group(1) if match else "-"
    for node in nodes.values():
        cursor: AgentNode | None = node
        while cursor is not None and cursor.parent is not None:
            if cursor.parent == root:
                node.branch = branch_of.get(cursor.agg, "-")
                break
            cursor = nodes.get(cursor.parent)
    return nodes


def _usage_costs(row: Any) -> tuple[float, float, dict[str, int]]:
    """Return (raw_usd, normalized_usd, token_classes) for one usage event row."""
    model = row["model"] or ""
    price = _price_for(model)
    prompt = int(row["prompt_tokens"])
    completion = int(row["completion_tokens"])
    cache_read = int(row["cache_read_tokens"])
    cache_write = int(row["cache_write_tokens"])

    openai_style = _is_openai_style(model)
    # Empirically validated against provider-reported cost_usd (see `check`):
    # every usage stream in this codebase reports prompt_tokens as the TOTAL
    # input, inclusive of cache_read (and cache_write for Anthropic rows) —
    # LiteLLM normalizes Anthropic usage to OpenAI-style totals.
    if prompt and prompt < cache_read + cache_write and model not in _SEMANTICS_WARNED:
        _SEMANTICS_WARNED.add(model)
        print(
            f"WARNING: {model or '<unknown model>'} row has prompt_tokens < "
            "cache_read + cache_write — a native-Anthropic (cache-exclusive) usage "
            "stream leaked in; uncached is clamped to 0 and costs for this model "
            "are suspect. Re-validate with `check`.",
            file=sys.stderr,
        )
    uncached = max(0, prompt - cache_read - cache_write)
    write_ratio = 0.0 if openai_style else ANTHROPIC_WRITE_RATIO

    raw = 0.0
    if price is not None:
        raw = (
            uncached * price.input_per_tok
            + cache_read * price.cache_read_per_tok
            + cache_write * price.cache_write_per_tok
            + completion * price.output_per_tok
        )
    ref = PRICES[REFERENCE_MODEL]
    normalized = (
        uncached + CACHE_READ_RATIO * cache_read + write_ratio * cache_write
    ) * ref.input_per_tok + completion * ref.output_per_tok
    tokens = {
        "uncached_input": uncached,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "completion": completion,
    }
    return raw, normalized, tokens


def _extract_read_path(tool_name: str, detail: str | None) -> str | None:
    """File path for a read-like worker tool call, else None.

    Only ``FileEditorAction`` with ``command: view`` (and the legacy
    ``read_file``/``file_editor`` shapes) is treated as a read. The
    ``ThoughtCaptured`` content is ``Tool: <name>\\nInput: {json}``; we parse
    the JSON and return its ``path`` when the command is a view/read.

    NOTE: recon probes (``ProbeStarted``/``ProbeCompleted`` read_file) carry
    only ``probe_type`` + ``result_summary`` in the event schema — no path —
    so recon-read redundancy is NOT measurable here and is excluded by design.
    """
    if not detail:
        return None
    lowered = tool_name.lower()
    if not any(k in lowered for k in ("file_editor", "fileeditor", "read")):
        return None
    json_match = re.search(r"\{.*\}", detail, flags=re.DOTALL)
    if not json_match:
        return None
    try:
        payload = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    command = payload.get("command")
    if command is not None and command not in ("view", "read"):
        return None  # str_replace / create / insert are writes, not reads
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def _gather(
    studies: set[str], latest: int | None = None,
) -> tuple[list[RunRef], dict[str, dict[str, AgentNode]], dict[str, list], dict[str, list], dict[str, list]]:
    repo_root = get_repo_root()
    refs = load_runs_for_studies(studies, repo_root, latest_per_cell_task=latest)
    conn = await _connect()
    trees: dict[str, dict[str, AgentNode]] = {}
    usages: dict[str, list] = {}
    toolcalls: dict[str, list] = {}
    walls: dict[str, list] = {}
    try:
        for ref in refs:
            tree = await _load_tree(conn, ref.run_id)
            aggs = [UUID(a) for a in tree]
            trees[ref.run_id] = tree
            usages[ref.run_id] = await conn.fetch(_USAGE_SQL, aggs)
            toolcalls[ref.run_id] = await conn.fetch(_TOOLCALL_SQL, aggs)
            walls[ref.run_id] = await conn.fetch(_RUN_WALL_SQL, [UUID(ref.run_id)])
    finally:
        await conn.close()
    return refs, trees, usages, toolcalls, walls


def _fmt_table(headers: list[str], rows: list[list[Any]]) -> str:
    widths = [len(h) for h in headers]
    rendered = [[str(c) for c in row] for row in rows]
    for row in rendered:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in rendered)
    return f"{line}\n{sep}\n{body}" if rows else f"{line}\n{sep}\n(no rows)"


def cmd_runs(refs, trees, usages, toolcalls, walls, as_json):
    rows = []
    for ref in refs:
        wall = walls[ref.run_id]
        rows.append(
            {
                "study": ref.study,
                "cell": ref.cell,
                "task": ref.task,
                "run_id": ref.run_id,
                "exit_status": ref.exit_status,
                "agents": len(trees[ref.run_id]),
                "wallclock_s": round(wall[0]["duration_seconds"], 1) if wall else None,
                "db_status": wall[0]["status"] if wall else None,
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    print(
        _fmt_table(
            ["study", "cell", "task", "run_id", "exit", "agents", "wall_s", "db_status"],
            [
                [r["study"], r["cell"], r["task"], r["run_id"][:8], r["exit_status"], r["agents"], r["wallclock_s"], r["db_status"]]
                for r in rows
            ],
        )
    )


def cmd_cost(refs, trees, usages, toolcalls, walls, as_json):
    agg: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for ref in refs:
        for row in usages[ref.run_id]:
            raw, norm, tokens = _usage_costs(row)
            key = (ref.study, ref.cell, (row["model"] or "?").split("/", 1)[-1])
            acc = agg[key]
            acc["raw_usd"] += raw
            acc["normalized_usd"] += norm
            acc["reported_usd"] += float(row["cost_usd"])
            for cls, count in tokens.items():
                acc[cls] += count
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for (study, cell, model), acc in agg.items():
        for field, value in acc.items():
            totals[(study, cell)][field] += value
        # Provider-neutral: discount the Anthropic orchestration tier by R.
        reported = acc["reported_usd"]
        totals[(study, cell)]["prov_norm_usd"] += (
            reported / PROVIDER_NORM_RATIO if _is_anthropic(model) else reported
        )

    if as_json:
        payload = [
            {"study": s, "cell": c, "model": m, **{k: round(v, 6) for k, v in acc.items()}}
            for (s, c, m), acc in sorted(agg.items())
        ] + [
            {"study": s, "cell": c, "model": "TOTAL", **{k: round(v, 6) for k, v in acc.items()}}
            for (s, c), acc in sorted(totals.items())
        ]
        print(json.dumps(payload, indent=2))
        return
    rows = []
    for (study, cell, model), acc in sorted(agg.items()):
        rows.append(
            [study, cell, model, int(acc["uncached_input"]), int(acc["cache_read"]), int(acc["cache_write"]), int(acc["completion"]), f"{acc['reported_usd']:.4f}", f"{acc['normalized_usd']:.4f}", ""]
        )
    for (study, cell), acc in sorted(totals.items()):
        rows.append(
            [study, cell, "TOTAL", int(acc["uncached_input"]), int(acc["cache_read"]), int(acc["cache_write"]), int(acc["completion"]), f"{acc['reported_usd']:.4f}", f"{acc['normalized_usd']:.4f}", f"{acc['prov_norm_usd']:.4f}"]
        )
    print(
        _fmt_table(
            ["study", "cell", "model", "uncached_in", "cache_read", "cache_write", "completion", "reported_usd", "normalized_usd", "prov_norm_usd"],
            rows,
        )
    )


def cmd_cache(refs, trees, usages, toolcalls, walls, as_json):
    agg: dict[tuple[str, str, str, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for ref in refs:
        tree = trees[ref.run_id]
        for row in usages[ref.run_id]:
            node = tree.get(str(row["aggregate_id"]))
            role = node.role if node else "?"
            depth = node.depth if node else -1
            _, _, tokens = _usage_costs(row)
            key = (ref.study, ref.cell, role, depth)
            for cls, count in tokens.items():
                agg[key][cls] += count
    rows = []
    payload = []
    for (study, cell, role, depth), acc in sorted(agg.items()):
        denom = acc["uncached_input"] + acc["cache_read"] + acc["cache_write"]
        hit = acc["cache_read"] / denom if denom else 0.0
        entry = {
            "study": study, "cell": cell, "role": role, "depth": depth,
            "uncached_input": int(acc["uncached_input"]),
            "cache_read": int(acc["cache_read"]),
            "cache_write": int(acc["cache_write"]),
            "cache_hit_rate": round(hit, 4),
        }
        payload.append(entry)
        rows.append([study, cell, role, depth, entry["uncached_input"], entry["cache_read"], entry["cache_write"], f"{hit:.1%}"])
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_fmt_table(["study", "cell", "role", "depth", "uncached_in", "cache_read", "cache_write", "hit_rate"], rows))


def cmd_tools(refs, trees, usages, toolcalls, walls, as_json):
    agg: dict[tuple[str, str, str, int, str], int] = defaultdict(int)
    for ref in refs:
        tree = trees[ref.run_id]
        for row in toolcalls[ref.run_id]:
            node = tree.get(str(row["aggregate_id"]))
            role = node.role if node else "?"
            depth = node.depth if node else -1
            agg[(ref.study, ref.cell, role, depth, row["tool_name"])] += 1
    if as_json:
        print(json.dumps([
            {"study": s, "cell": c, "role": r, "depth": d, "tool": t, "calls": n}
            for (s, c, r, d, t), n in sorted(agg.items())
        ], indent=2))
        return
    rows = [[s, c, r, d, t, n] for (s, c, r, d, t), n in sorted(agg.items())]
    print(_fmt_table(["study", "cell", "role", "depth", "tool", "calls"], rows))


def cmd_files(refs, trees, usages, toolcalls, walls, as_json):
    payload = []
    for ref in refs:
        tree = trees[ref.run_id]
        readers: dict[str, set[str]] = defaultdict(set)
        branch_readers: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in toolcalls[ref.run_id]:
            path = _extract_read_path(row["tool_name"], row["detail"])
            if not path:
                continue
            agg_id = str(row["aggregate_id"])
            node = tree.get(agg_id)
            readers[path].add(agg_id)
            if node is not None:
                branch_readers[(node.branch, path)].add(agg_id)
        dup_run = {p: len(a) for p, a in readers.items() if len(a) > 1}
        dup_branch = {
            f"{branch}:{path}": len(agents)
            for (branch, path), agents in branch_readers.items()
            if len(agents) > 1
        }
        payload.append(
            {
                "study": ref.study,
                "cell": ref.cell,
                "task": ref.task,
                "run_id": ref.run_id,
                "files_read": len(readers),
                "files_read_by_multiple_agents": len(dup_run),
                "redundant_read_ratio": round(len(dup_run) / len(readers), 4) if readers else 0.0,
                "duplicates_within_branch": len(dup_branch),
                "worst_offenders": sorted(dup_run.items(), key=lambda kv: -kv[1])[:10],
            }
        )
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    rows = [
        [p["study"], p["cell"], p["task"], p["run_id"][:8], p["files_read"], p["files_read_by_multiple_agents"], f"{p['redundant_read_ratio']:.1%}", p["duplicates_within_branch"]]
        for p in payload
    ]
    print(_fmt_table(["study", "cell", "task", "run", "files", "multi-agent", "redundant%", "branch-dups"], rows))


def cmd_check(refs, trees, usages, toolcalls, walls, as_json):
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for ref in refs:
        for row in usages[ref.run_id]:
            raw, _, _ = _usage_costs(row)
            model = (row["model"] or "?").split("/", 1)[-1]
            agg[model]["recomputed_usd"] += raw
            agg[model]["reported_usd"] += float(row["cost_usd"])
    payload = []
    for model, acc in sorted(agg.items()):
        reported = acc["reported_usd"]
        recomputed = acc["recomputed_usd"]
        drift = (recomputed - reported) / reported if reported else None
        payload.append(
            {
                "model": model,
                "recomputed_usd": round(recomputed, 4),
                "reported_usd": round(reported, 4),
                "drift": round(drift, 4) if drift is not None else None,
            }
        )
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    print(
        _fmt_table(
            ["model", "recomputed_usd", "reported_usd", "drift"],
            [[p["model"], p["recomputed_usd"], p["reported_usd"], p["drift"]] for p in payload],
        )
    )


_PROMPTS_SQL = """
SELECT payload->>'prompt' AS prompt
FROM events
WHERE aggregate_id = $1 AND event_type = 'PromptSent'
  AND payload->>'prompt_type' = 'worker_execution'
ORDER BY sequence_number
"""

_JUDGE_SQL = """
SELECT count(*) AS n
FROM events
WHERE aggregate_id = ANY($1::uuid[])
  AND (
        (event_type = 'TokensConsumed' AND payload->>'operation' ILIKE '%judge%')
     OR payload->>'failed_stage' = 'judge'
  )
"""

_COST_ENV_SQL = """
SELECT aggregate_id,
       payload->>'model' AS model,
       payload->>'container_id' AS container_id,
       payload->>'conversation_id' AS conversation_id
FROM events
WHERE aggregate_id = ANY($1::uuid[]) AND event_type = 'WorkerCostRecorded'
"""

_LLM_MODELS_SQL = """
SELECT DISTINCT payload->>'model' AS model
FROM events
WHERE aggregate_id = ANY($1::uuid[]) AND event_type = 'TokensConsumed'
"""

_SOURCEFILE_SQL = """
SELECT event_type, count(*) AS n
FROM events
WHERE aggregate_id = ANY($1::uuid[])
  AND event_type IN ('SourceFileObserved', 'SourceFileEdited')
GROUP BY event_type
"""


def _check_flat_prompt(prompts: list[str]) -> tuple[str, str]:
    if not prompts:
        return "FAIL", "no worker_execution PromptSent on the root"
    problems: list[str] = []
    for prompt in prompts:
        lowered = prompt.lower()
        hits = sorted({term for term in FLAT_PROMPT_BLACKLIST if term in lowered})
        missing = sorted({m for m in FLAT_PROMPT_REQUIRED if m not in prompt})
        if hits:
            problems.append(f"forbidden vocabulary {hits}")
        if missing:
            problems.append(f"missing anchors {missing}")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"{len(prompts)} prompt(s) clean"


def _check_run_invariants(ref: RunRef, tree: dict[str, AgentNode], data: dict) -> list[dict]:
    checks: list[dict] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append(
            {"study": ref.study, "cell": ref.cell, "task": ref.task,
             "run_id": ref.run_id, "check": check, "status": status, "detail": detail}
        )

    if len(tree) == 1:  # flat (single-agent) run: prompt hygiene applies
        status, detail = _check_flat_prompt(data["prompts"])
        add("flat_prompt_clean", status, detail)

    add(
        "no_judge_llm",
        "PASS" if data["judge_count"] == 0 else "FAIL",
        f"{data['judge_count']} judge-stage event(s)",
    )

    models = sorted(
        {m for m in data["llm_models"] if m}
        | {row["model"] for row in data["cost_rows"] if row["model"]}
    )
    opus = [m for m in models if "opus" in m.lower()]
    add("models_allowed", "FAIL" if opus else "PASS", f"models={models}")

    cost_rows = data["cost_rows"]
    if cost_rows:
        per_agg: dict[str, int] = defaultdict(int)
        for row in cost_rows:
            per_agg[str(row["aggregate_id"])] += 1
        extras = {agg: n for agg, n in per_agg.items() if n != 1}
        add(
            "one_cost_event_per_worker",
            "PASS" if not extras else "FAIL",
            f"{len(per_agg)} worker(s), duplicates={extras or 'none'}",
        )

        containers = {row["container_id"] for row in cost_rows if row["container_id"]}
        if not containers:
            add("one_container", "UNKNOWN", "no container_id on events (pre-capture runs)")
        else:
            add(
                "one_container",
                "PASS" if len(containers) == 1 else "FAIL",
                f"{len(containers)} distinct container(s)",
            )

        convs = [row["conversation_id"] for row in cost_rows if row["conversation_id"]]
        if not convs:
            add("fresh_conversations", "UNKNOWN", "no conversation_id on events (pre-capture runs)")
        else:
            add(
                "fresh_conversations",
                "PASS" if len(set(convs)) == len(convs) else "FAIL",
                f"{len(set(convs))} distinct conversation(s) across {len(convs)} worker(s)",
            )

    observed = data["sourcefiles"].get("SourceFileObserved", 0)
    edited = data["sourcefiles"].get("SourceFileEdited", 0)
    add("shared_code_capture", "INFO", f"SourceFileObserved={observed} SourceFileEdited={edited}")
    return checks


async def _invariants_gather(refs: list[RunRef], trees: dict[str, dict[str, AgentNode]]) -> dict[str, dict]:
    conn = await _connect()
    out: dict[str, dict] = {}
    try:
        for ref in refs:
            aggs = [UUID(a) for a in trees[ref.run_id]]
            prompts = [
                r["prompt"]
                for r in await conn.fetch(_PROMPTS_SQL, UUID(ref.run_id))
                if r["prompt"]
            ]
            judge_row = await conn.fetchrow(_JUDGE_SQL, aggs)
            judge_count = judge_row["n"] if judge_row else 0
            cost_rows = await conn.fetch(_COST_ENV_SQL, aggs)
            llm_models = [r["model"] for r in await conn.fetch(_LLM_MODELS_SQL, aggs)]
            sourcefiles = {
                r["event_type"]: r["n"] for r in await conn.fetch(_SOURCEFILE_SQL, aggs)
            }
            out[ref.run_id] = {
                "prompts": prompts,
                "judge_count": judge_count,
                "cost_rows": cost_rows,
                "llm_models": llm_models,
                "sourcefiles": sourcefiles,
            }
    finally:
        await conn.close()
    return out


def cmd_invariants(refs, trees, usages, toolcalls, walls, as_json):
    data = asyncio.run(_invariants_gather(refs, trees))
    checks = [
        check
        for ref in refs
        for check in _check_run_invariants(ref, trees[ref.run_id], data[ref.run_id])
    ]
    if as_json:
        print(json.dumps(checks, indent=2))
    else:
        print(
            _fmt_table(
                ["study", "cell", "task", "run", "check", "status", "detail"],
                [
                    [c["study"], c["cell"], c["task"], c["run_id"][:8], c["check"], c["status"], c["detail"]]
                    for c in checks
                ],
            )
        )
    failures = [c for c in checks if c["status"] == "FAIL"]
    if failures:
        raise SystemExit(1)


COMMANDS = {
    "runs": cmd_runs,
    "cost": cmd_cost,
    "cache": cmd_cache,
    "tools": cmd_tools,
    "files": cmd_files,
    "check": cmd_check,
    "invariants": cmd_invariants,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="study_sql", description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--studies", required=True, help="Comma-separated study ids")
    parser.add_argument(
        "--latest", type=int, default=None,
        help="Keep only the N most-recent runs per (study,cell,task) by manifest mtime — "
             "isolates one re-run cycle from prior runs of the same study_id.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    studies = {s.strip() for s in args.studies.split(",") if s.strip()}
    refs, trees, usages, toolcalls, walls = asyncio.run(_gather(studies, latest=args.latest))
    if not refs:
        raise SystemExit(f"no runs found for studies {sorted(studies)} under runs/")
    COMMANDS[args.command](refs, trees, usages, toolcalls, walls, args.as_json)


if __name__ == "__main__":
    main()
