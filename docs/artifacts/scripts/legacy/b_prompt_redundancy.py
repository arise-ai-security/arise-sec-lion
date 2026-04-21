"""Prompt-level redundancy analysis for Pillar B (tree-vs-flat).

Question: in B1/B2, when a run spawns multiple agents, how much of each
agent's first ``prompt_sent.prompt_text`` overlaps with the prompts sent
to its siblings / to its parent? Tokeniser is ``tiktoken`` ``cl100k_base``
(same proxy used for CNR in report-0420 §3.4.1).

Outputs a compact JSON summary to stdout and a per-cell CSV of the
three families of pairwise Jaccard similarities:

* sibling (same parent, first prompt of each)
* hierarchy (parent -> child first prompt)
* intra-agent (prompt N vs prompt 1 within one agent; only computed when
  an agent receives >1 prompts — in the v2-20260420 runs every agent
  receives exactly one prompt, so this is reported as "not computable").

The script also prints one concrete example (two real prompt texts
side-by-side) so the headline number is grounded.

Run:
    uv run python docs/pillar_b/scripts/b_prompt_redundancy.py
"""

from __future__ import annotations

import csv
import json
import logging
import statistics
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Iterable

import tiktoken


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "dataset-v2-20260420" / "runs"
OUT_DIR = REPO_ROOT / "docs" / "pillar_b" / "tables"
OUT_CSV = OUT_DIR / "b_prompt_redundancy_pairs.csv"
OUT_JSON = OUT_DIR / "b_prompt_redundancy_summary.json"

PRIMARY_CVES = (
    "njs.cve-2022-32414",
    "njs.cve-2022-38890",
    "faad2.cve-2021-32272",
    "faad2.cve-2018-20196",
    "mruby.cve-2022-0240",
    "gpac.cve-2022-1795",
    "gpac.cve-2021-40575",
    "openjpeg.cve-2016-7445",
    "imagemagick.cve-2019-13309",
    "exiv2.cve-2017-14859",
)
CELLS = ("B1", "B2")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("prompt_redundancy")


def _tokeniser() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def _iter_events(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def jaccard(a: set[int], b: set[int]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_run(run_dir: Path, enc: tiktoken.Encoding) -> dict:
    """Extract agent topology + first prompt token set per agent."""
    agent_parent: dict[str, str | None] = {}
    agent_role: dict[str, str] = {}
    # For each agent, collect all prompts in order; then take the first for
    # sibling/hierarchy and compare prompt-N vs prompt-1 for intra.
    prompts_by_agent: dict[str, list[str]] = defaultdict(list)

    for ev in _iter_events(run_dir / "events.jsonl"):
        et = ev.get("event_type")
        if et == "agent_created":
            aid = ev.get("agent_id")
            if aid is None:
                continue
            agent_parent[aid] = ev.get("parent_agent_id")
            agent_role[aid] = ev.get("role") or (ev.get("payload") or {}).get("role", "?")
        elif et == "prompt_sent":
            aid = ev.get("agent_id")
            if aid is None:
                continue
            text = (ev.get("payload") or {}).get("prompt_text") or ""
            if text:
                prompts_by_agent[aid].append(text)

    token_sets_first: dict[str, set[int]] = {}
    token_sets_all: dict[str, list[set[int]]] = {}
    first_text: dict[str, str] = {}
    for aid, texts in prompts_by_agent.items():
        token_sets_all[aid] = []
        for t in texts:
            try:
                toks = set(enc.encode(t, disallowed_special=()))
            except Exception:  # noqa: BLE001
                toks = set()
            token_sets_all[aid].append(toks)
        if texts:
            token_sets_first[aid] = token_sets_all[aid][0]
            first_text[aid] = texts[0]

    return {
        "agent_parent": agent_parent,
        "agent_role": agent_role,
        "prompts_by_agent": prompts_by_agent,
        "token_sets_first": token_sets_first,
        "token_sets_all": token_sets_all,
        "first_text": first_text,
    }


def sibling_pairs(run: dict) -> list[tuple[str, str, float]]:
    """All pairwise Jaccard values between first prompts of siblings."""
    groups: dict[str | None, list[str]] = defaultdict(list)
    for aid, pid in run["agent_parent"].items():
        if aid in run["token_sets_first"]:
            groups[pid].append(aid)
    out: list[tuple[str, str, float]] = []
    for pid, children in groups.items():
        if pid is None or len(children) < 2:
            continue
        for a, b in combinations(children, 2):
            j = jaccard(run["token_sets_first"][a], run["token_sets_first"][b])
            out.append((a, b, j))
    return out


def _nearest_prompted_ancestor(aid: str, run: dict) -> str | None:
    """Walk up agent_parent chain until an agent that has a first prompt.

    B2 uses BOSS (prompted) -> MANAGER (orchestrator, no LLM prompt) ->
    WORKER (prompted), so direct parent->child pairs skip a level.
    """
    cur = run["agent_parent"].get(aid)
    while cur is not None:
        if cur in run["token_sets_first"]:
            return cur
        cur = run["agent_parent"].get(cur)
    return None


def hierarchy_pairs(run: dict) -> list[tuple[str, str, float]]:
    """Jaccard between a prompted agent's first prompt and that of its
    nearest prompted ancestor.
    """
    out: list[tuple[str, str, float]] = []
    for aid in run["token_sets_first"]:
        anc = _nearest_prompted_ancestor(aid, run)
        if anc is None:
            continue
        j = jaccard(run["token_sets_first"][anc], run["token_sets_first"][aid])
        out.append((anc, aid, j))
    return out


def intra_pairs(run: dict) -> list[tuple[str, int, float]]:
    """For any agent with >1 prompts, Jaccard(prompt_N, prompt_1)."""
    out: list[tuple[str, int, float]] = []
    for aid, ts in run["token_sets_all"].items():
        if len(ts) < 2:
            continue
        base = ts[0]
        for idx in range(1, len(ts)):
            out.append((aid, idx, jaccard(base, ts[idx])))
    return out


def summarise(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    enc = _tokeniser()

    per_cell_values: dict[str, dict[str, list[float]]] = {
        cell: {"sibling": [], "hierarchy": [], "intra": []} for cell in CELLS
    }
    per_cell_runs_with_siblings: dict[str, int] = {c: 0 for c in CELLS}
    per_cell_runs_with_hierarchy: dict[str, int] = {c: 0 for c in CELLS}
    per_cell_runs_with_intra: dict[str, int] = {c: 0 for c in CELLS}

    csv_rows: list[dict] = []

    # Track best-example sibling pair (closest to median sibling Jaccard in
    # each cell) for the side-by-side excerpt in the fragment.
    sibling_examples: dict[str, list[tuple[float, str, str, str, str, str]]] = {
        c: [] for c in CELLS
    }

    for cve in PRIMARY_CVES:
        for cell in CELLS:
            run_dir = RUNS_ROOT / cve / cell / "0"
            if not (run_dir / "events.jsonl").exists():
                logger.info("skipping missing run: %s/%s", cve, cell)
                continue
            run = load_run(run_dir, enc)

            sib = sibling_pairs(run)
            hie = hierarchy_pairs(run)
            ia = intra_pairs(run)

            if sib:
                per_cell_runs_with_siblings[cell] += 1
            if hie:
                per_cell_runs_with_hierarchy[cell] += 1
            if ia:
                per_cell_runs_with_intra[cell] += 1

            for a, b, j in sib:
                per_cell_values[cell]["sibling"].append(j)
                csv_rows.append(
                    {
                        "cve_id": cve,
                        "cell": cell,
                        "pair_type": "sibling",
                        "agent_a": a,
                        "role_a": run["agent_role"].get(a, "?"),
                        "agent_b": b,
                        "role_b": run["agent_role"].get(b, "?"),
                        "jaccard": j,
                    }
                )
                sibling_examples[cell].append(
                    (j, cve, a, run["agent_role"].get(a, "?"), b, run["agent_role"].get(b, "?"))
                )
            for a, b, j in hie:
                per_cell_values[cell]["hierarchy"].append(j)
                csv_rows.append(
                    {
                        "cve_id": cve,
                        "cell": cell,
                        "pair_type": "hierarchy",
                        "agent_a": a,
                        "role_a": run["agent_role"].get(a, "?"),
                        "agent_b": b,
                        "role_b": run["agent_role"].get(b, "?"),
                        "jaccard": j,
                    }
                )
            for a, idx, j in ia:
                per_cell_values[cell]["intra"].append(j)
                csv_rows.append(
                    {
                        "cve_id": cve,
                        "cell": cell,
                        "pair_type": "intra",
                        "agent_a": a,
                        "role_a": run["agent_role"].get(a, "?"),
                        "agent_b": f"prompt_{idx}_vs_prompt_0",
                        "role_b": "",
                        "jaccard": j,
                    }
                )

    # Write CSV
    if csv_rows:
        with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(
                fh,
                fieldnames=[
                    "cve_id",
                    "cell",
                    "pair_type",
                    "agent_a",
                    "role_a",
                    "agent_b",
                    "role_b",
                    "jaccard",
                ],
            )
            w.writeheader()
            w.writerows(csv_rows)
        logger.info("wrote %s (%d rows)", OUT_CSV, len(csv_rows))

    # Build summary
    summary: dict = {
        "tokeniser": "tiktoken.cl100k_base",
        "similarity": "token-set Jaccard on first prompt_sent.prompt_text per agent",
        "primary_cves": list(PRIMARY_CVES),
        "cells": {},
    }
    for cell in CELLS:
        summary["cells"][cell] = {
            "sibling": {
                **summarise(per_cell_values[cell]["sibling"]),
                "n_runs_contributing": per_cell_runs_with_siblings[cell],
            },
            "hierarchy": {
                **summarise(per_cell_values[cell]["hierarchy"]),
                "n_runs_contributing": per_cell_runs_with_hierarchy[cell],
            },
            "intra_agent": {
                **summarise(per_cell_values[cell]["intra"]),
                "n_runs_contributing": per_cell_runs_with_intra[cell],
                "note": (
                    "In v2-20260420 every agent has exactly one prompt_sent "
                    "event; Jaccard(prompt_N, prompt_1) is therefore not "
                    "computable from this data. CNR (§3.4.1) provides the "
                    "closest proxy."
                )
                if per_cell_values[cell]["intra"] == []
                else "",
            },
        }

    # Pick concrete example: the sibling pair whose Jaccard is closest to the
    # median of sibling Jaccards in B2 (the cell the reader cares about most)
    # and attach both prompt texts.
    example: dict | None = None
    cell_for_example = "B2"
    vals = per_cell_values[cell_for_example]["sibling"]
    if vals:
        median = statistics.median(vals)
        exs = sibling_examples[cell_for_example]
        _, cve, a, role_a, b, role_b = min(exs, key=lambda t: abs(t[0] - median))
        # Reload the texts
        run = load_run(RUNS_ROOT / cve / cell_for_example / "0", enc)
        example = {
            "cell": cell_for_example,
            "cve_id": cve,
            "agent_a_id": a,
            "agent_a_role": role_a,
            "agent_b_id": b,
            "agent_b_role": role_b,
            "jaccard": jaccard(
                run["token_sets_first"][a], run["token_sets_first"][b]
            ),
            "agent_a_prompt": run["first_text"][a],
            "agent_b_prompt": run["first_text"][b],
        }
    summary["example_sibling_pair"] = example

    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    logger.info("wrote %s", OUT_JSON)

    # Pretty print short report to stdout
    def fmt(d: dict) -> str:
        if d["n"] == 0:
            return "n=0 (no data)"
        return (
            f"n={d['n']} mean={d['mean']:.3f} median={d['median']:.3f} "
            f"min={d['min']:.3f} max={d['max']:.3f}"
        )

    print("\n=== Prompt redundancy summary ===")
    for cell in CELLS:
        c = summary["cells"][cell]
        print(f"\n[{cell}]")
        print(
            f"  sibling   : {fmt(c['sibling'])} "
            f"({c['sibling']['n_runs_contributing']}/10 runs contributing)"
        )
        print(
            f"  hierarchy : {fmt(c['hierarchy'])} "
            f"({c['hierarchy']['n_runs_contributing']}/10 runs contributing)"
        )
        print(
            f"  intra     : {fmt(c['intra_agent'])} "
            f"({c['intra_agent']['n_runs_contributing']}/10 runs contributing)"
        )
        if c["intra_agent"].get("note"):
            print(f"    note: {c['intra_agent']['note']}")

    if example:
        print(
            f"\nExample sibling pair (B2, {example['cve_id']}): "
            f"{example['agent_a_role']} vs {example['agent_b_role']}, "
            f"Jaccard={example['jaccard']:.3f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
