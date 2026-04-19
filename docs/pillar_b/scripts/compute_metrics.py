"""Compute all Pillar B metrics and pre-registered comparisons.

Outputs (all under ``docs/pillar_b/tables/``):
* ``index_enriched.csv`` — INDEX.jsonl rows + stratum, patch_on_disk,
  no_patch_produced, CNR mean, tool-call redundancy metrics,
  corrected_mechanical_pass (from A-cell sensitivity).
* ``comparison_results.csv`` — row per pre-registered comparison
  (H1/S1/S4/S5/C1/C2) with statistic, p-value, effect size, CI.
* ``per_cve_breakdown.csv`` — one row per (CVE, cell) with all metrics.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
import tiktoken
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ROOT = REPO_ROOT / "dataset"
RUNS_ROOT = DATASET_ROOT / "runs"
INDEX_PATH = DATASET_ROOT / "INDEX.jsonl"
LOCKED_PATH = DATASET_ROOT / "locked_instances.yaml"
TABLES_DIR = REPO_ROOT / "docs" / "pillar_b" / "tables"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("metrics")


CELLS_ORDERED = ("A1", "A2", "A3", "A4", "B1", "B2")
PAIRED_COMPARISONS = [
    ("H1", "A1", "B2"),
    ("S1", "B1", "B2"),
    ("S4", "A2", "B1"),
    ("S5", "A3", "B2"),
]


def _tokenizer() -> tiktoken.Encoding:
    # tiktoken's ``cl100k_base`` is the GPT-4/Claude-approximate tokenizer. We
    # document the approximation in the report — Anthropic does not ship a
    # free tokenizer for Claude 4.6/4.7, so cl100k is a stable proxy.
    return tiktoken.get_encoding("cl100k_base")


def load_locked() -> dict[str, dict]:
    raw = yaml.safe_load(LOCKED_PATH.read_text(encoding="utf-8"))
    return {item["instance_id"]: item for item in raw["instances"]}


def load_index() -> list[dict]:
    return [
        json.loads(line)
        for line in INDEX_PATH.read_text().splitlines()
        if line.strip()
    ]


def _iter_events(run_dir: Path):
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return
    for line in events_path.open(encoding="utf-8"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


@dataclass
class RunMetrics:
    run_id: str
    cnr_mean: float | None = None
    cnr_n_prompts: int = 0
    tool_calls_total: int = 0
    tool_calls_by_tool: dict[str, int] | None = None
    redundancy_intra: int = 0
    redundancy_sibling: int = 0
    redundancy_hierarchy: int = 0
    redundancy_rate_total: float | None = None
    security_tool_adoption: bool = False
    security_tool_invocations: int = 0


def _target_from_tool_input(tool_name: str, tool_input: dict) -> str | None:
    """Derive a normalized 'target' string for a tool call, per spec §5.3."""
    if not isinstance(tool_input, dict) or not tool_input:
        return None
    if tool_name in {"Read", "Edit", "Write"}:
        fp = tool_input.get("file_path")
        if isinstance(fp, str):
            return fp
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if isinstance(cmd, str):
            toks = cmd.strip().split()
            return " ".join(toks[:3])
    if tool_name in {"Grep", "Glob"}:
        pat = tool_input.get("pattern") or tool_input.get("query")
        if isinstance(pat, str):
            return pat
    return None


def compute_run_metrics(run_dir: Path, cell: str) -> RunMetrics:
    rm = RunMetrics(run_id=run_dir.parent.parent.name + "-" + cell + "-" + run_dir.name)
    enc = _tokenizer()
    # Tool-call scans
    tool_name_counter: Counter[str] = Counter()
    # (agent_id, tool, target) for intra
    per_agent_calls: dict[str, list[tuple[str, str]]] = defaultdict(list)
    # For sibling/hierarchy we need agent tree topology
    agent_parent: dict[str, str | None] = {}
    agent_role: dict[str, str] = {}
    agent_depth: dict[str, int] = {}
    # CNR
    cumulative_tokens: set[int] = set()
    cnr_values: list[float] = []
    # Security tool scan
    security_hits = 0

    for ev in _iter_events(run_dir):
        et = ev["event_type"]
        payload = ev.get("payload", {}) or {}
        if et == "agent_created":
            aid = ev.get("agent_id")
            pid = ev.get("parent_agent_id")
            if aid is not None:
                agent_parent[aid] = pid
                agent_role[aid] = ev.get("role", "?")
                agent_depth[aid] = int(ev.get("depth", 0))
        elif et == "prompt_sent":
            text = payload.get("prompt_text") or ""
            if text:
                try:
                    toks = enc.encode(text, disallowed_special=())
                except Exception:  # noqa: BLE001
                    toks = []
                tok_set = set(toks)
                if not tok_set:
                    continue
                novel = tok_set - cumulative_tokens
                cnr = len(novel) / len(tok_set)
                cnr_values.append(cnr)
                cumulative_tokens.update(tok_set)
        elif et == "tool_use":
            # Filter out synthetic tool_use events that are really tree domain
            # events wrapped as tool_use in the events.jsonl schema.
            if isinstance(payload.get("data"), dict):
                continue
            tn = payload.get("tool_name")
            if not tn or tn == "?":
                continue
            tool_name_counter[tn] += 1
            target = _target_from_tool_input(tn, payload.get("tool_input") or {})
            aid = ev.get("agent_id") or "FLAT"
            if target is not None:
                per_agent_calls[aid].append((tn, target))
            # security-tool heuristic
            if tn == "Bash":
                cmd = (payload.get("tool_input") or {}).get("command", "")
                if isinstance(cmd, str) and ("valgrind" in cmd or "klee" in cmd):
                    security_hits += 1
        elif et == "tokens_consumed":
            pass  # aggregated via meta; no per-event cost needed here

    rm.tool_calls_total = sum(tool_name_counter.values())
    rm.tool_calls_by_tool = dict(tool_name_counter)
    if cnr_values:
        rm.cnr_mean = mean(cnr_values)
        rm.cnr_n_prompts = len(cnr_values)

    # Redundancy computation
    def norm(pair: tuple[str, str]) -> tuple[str, str]:
        return pair

    # intra
    intra_total = 0
    for aid, calls in per_agent_calls.items():
        c = Counter(calls)
        intra_total += sum(v - 1 for v in c.values() if v > 1)
    rm.redundancy_intra = intra_total

    # sibling
    sibling_groups: dict[str | None, list[str]] = defaultdict(list)
    for aid, pid in agent_parent.items():
        sibling_groups[pid].append(aid)
    sibling_total = 0
    for pid, children in sibling_groups.items():
        if len(children) < 2:
            continue
        # Count (tool,target) pairs that appear in ≥2 children
        pair_children: dict[tuple[str, str], set[str]] = defaultdict(set)
        for c in children:
            for pair in per_agent_calls.get(c, []):
                pair_children[pair].add(c)
        for pair, cs in pair_children.items():
            if len(cs) >= 2:
                sibling_total += len(cs) - 1
    rm.redundancy_sibling = sibling_total

    # hierarchy: pair appearing in child after any ancestor
    def _ancestors(aid: str) -> list[str]:
        out, cur = [], agent_parent.get(aid)
        while cur is not None:
            out.append(cur)
            cur = agent_parent.get(cur)
        return out

    hierarchy_total = 0
    for aid, calls in per_agent_calls.items():
        ancestor_pairs: set[tuple[str, str]] = set()
        for anc in _ancestors(aid):
            ancestor_pairs.update(per_agent_calls.get(anc, []))
        for pair in calls:
            if pair in ancestor_pairs:
                hierarchy_total += 1
    rm.redundancy_hierarchy = hierarchy_total

    if rm.tool_calls_total:
        rm.redundancy_rate_total = (
            (rm.redundancy_intra + rm.redundancy_sibling + rm.redundancy_hierarchy)
            / rm.tool_calls_total
        )
    rm.security_tool_adoption = security_hits > 0
    rm.security_tool_invocations = security_hits
    return rm


# --- Statistics ---------------------------------------------------------


def mcnemar_paired(
    pairs: list[tuple[int, int]],
) -> dict[str, Any]:
    """Exact McNemar test for paired binary outcomes. pairs = list of (x, y).

    Returns: n, b, c, discordant, p_exact (two-sided exact binomial on
    discordant pairs), diff_rate (y - x), bootstrap 95% CI for diff_rate.
    """
    from scipy.stats import binomtest

    n = len(pairs)
    b = sum(1 for x, y in pairs if x == 0 and y == 1)  # y wins
    c = sum(1 for x, y in pairs if x == 1 and y == 0)  # x wins
    disc = b + c
    p = binomtest(k=b, n=disc, p=0.5, alternative="two-sided").pvalue if disc else 1.0
    diff = (sum(y for _, y in pairs) - sum(x for x, _ in pairs)) / max(n, 1)
    # bootstrap CI on diff_rate
    rng = np.random.default_rng(42)
    if n == 0:
        lo, hi = 0.0, 0.0
    else:
        arr = np.array(pairs, dtype=float)
        boots = []
        for _ in range(10_000):
            idx = rng.integers(0, n, size=n)
            s = arr[idx]
            boots.append((s[:, 1].mean() - s[:, 0].mean()))
        lo, hi = np.percentile(boots, [2.5, 97.5])
    return {
        "n_pairs": n,
        "b_y_wins": b,
        "c_x_wins": c,
        "n_discordant": disc,
        "p_exact": float(p),
        "diff_rate": float(diff),
        "ci_lo": float(lo),
        "ci_hi": float(hi),
    }


def kruskal_by_cell(
    values_by_cell: dict[str, list[float]],
) -> dict[str, Any]:
    """Kruskal-Wallis across cells + Dunn pairwise without correction."""
    from scipy.stats import kruskal, mannwhitneyu

    cells = [c for c in values_by_cell if values_by_cell[c]]
    if len(cells) < 2:
        return {"H": None, "p": None, "cells": cells}
    samples = [values_by_cell[c] for c in cells]
    H, p = kruskal(*samples)
    pairwise = {}
    for i, ci in enumerate(cells):
        for j, cj in enumerate(cells):
            if j <= i:
                continue
            try:
                u, pp = mannwhitneyu(
                    values_by_cell[ci], values_by_cell[cj], alternative="two-sided"
                )
                pairwise[f"{ci}_vs_{cj}"] = float(pp)
            except ValueError:
                pairwise[f"{ci}_vs_{cj}"] = None
    # medians
    medians = {c: float(np.median(values_by_cell[c])) for c in cells}
    return {
        "H": float(H),
        "p": float(p),
        "cells": cells,
        "medians": medians,
        "pairwise": pairwise,
    }


def bh_fdr(p_values: list[float], alpha: float = 0.1) -> list[bool]:
    """Benjamini-Hochberg. Returns reject-null booleans in input order."""
    from statsmodels.stats.multitest import multipletests

    rej, _, _, _ = multipletests(p_values, alpha=alpha, method="fdr_bh")
    return rej.tolist()


def enrich_index(
    index_rows: list[dict],
    locked: dict[str, dict],
    metrics_by_run: dict[str, RunMetrics],
) -> list[dict]:
    """Return a copy of index_rows with extra columns."""
    out = []
    for r in index_rows:
        rr = dict(r)
        entry = locked.get(r["cve_id"], {})
        rr["stratum"] = entry.get("stratum", "UNKNOWN")
        rr["project"] = entry.get("project", r["cve_id"].split(".", 1)[0])
        run_dir = Path(r["path"])
        if not run_dir.is_absolute():
            run_dir = REPO_ROOT / run_dir
        # Retrofit info
        has_patch = (run_dir / "workspace" / "model_patch.diff").exists()
        rr["workspace_has_patch"] = has_patch
        rr["no_patch_produced"] = r["cell"] in {"B1", "B2"} and not has_patch
        # Corrected mechanical (A-cell sensitivity)
        cm_path = run_dir / "corrected_mechanical.json"
        if cm_path.exists():
            try:
                cm = json.loads(cm_path.read_text())
                rr["corrected_mechanical_builder"] = bool(cm.get("builder_pass"))
                rr["corrected_mechanical_exploiter"] = bool(cm.get("exploiter_pass"))
                rr["corrected_mechanical_fixer"] = bool(cm.get("fixer_pass"))
                rr["corrected_mechanical_end_to_end"] = bool(cm.get("end_to_end_pass"))
            except json.JSONDecodeError:
                pass
        else:
            rr["corrected_mechanical_builder"] = None
            rr["corrected_mechanical_exploiter"] = None
            rr["corrected_mechanical_fixer"] = None
            rr["corrected_mechanical_end_to_end"] = None
        # Run metrics
        m = metrics_by_run.get(r["run_id"])
        if m is not None:
            rr["cnr_mean"] = m.cnr_mean
            rr["cnr_n_prompts"] = m.cnr_n_prompts
            rr["tool_calls_total"] = m.tool_calls_total
            rr["tool_calls_by_tool"] = json.dumps(m.tool_calls_by_tool or {})
            rr["redundancy_intra"] = m.redundancy_intra
            rr["redundancy_sibling"] = m.redundancy_sibling
            rr["redundancy_hierarchy"] = m.redundancy_hierarchy
            rr["redundancy_rate_total"] = m.redundancy_rate_total
            rr["security_tool_adoption"] = m.security_tool_adoption
            rr["security_tool_invocations"] = m.security_tool_invocations
        # Flatten mechanical_pass + define effective_pass_after_retrofit
        mp = r.get("mechanical_pass") or {}
        rr["mech_builder"] = bool(mp.get("builder"))
        rr["mech_exploiter"] = bool(mp.get("exploiter"))
        rr["mech_fixer"] = bool(mp.get("fixer"))
        rr["mech_end_to_end"] = bool(mp.get("end_to_end"))
        # Effective end_to_end: for B, use the retrofitted INDEX value (already
        # updated). For A, prefer the corrected_mechanical value if present
        # (sensitivity), else the original.
        if r["cell"].startswith("B"):
            rr["effective_pass_end_to_end"] = rr["mech_end_to_end"]
        else:
            if rr["corrected_mechanical_end_to_end"] is not None:
                rr["effective_pass_end_to_end"] = rr["corrected_mechanical_end_to_end"]
            else:
                rr["effective_pass_end_to_end"] = rr["mech_end_to_end"]
        out.append(rr)
    return out


def run_comparisons(enriched: list[dict]) -> list[dict]:
    """Run pre-registered comparisons and return list of result dicts."""
    out: list[dict] = []
    # Index by (cve, cell) → row
    by_key: dict[tuple[str, str], dict] = {}
    for r in enriched:
        if r["cell"].startswith("A1") or r["cell"] in {"A2", "A3", "A4", "B1", "B2"}:
            by_key.setdefault((r["cve_id"], r["cell"]), r)

    # Paired binary (H1/S1/S4/S5) on effective_pass_end_to_end
    comparison_rows = []
    p_values_for_fdr: list[tuple[str, float]] = []
    for cmp_id, cell_x, cell_y in PAIRED_COMPARISONS:
        pairs: list[tuple[int, int]] = []
        cves_used: list[str] = []
        for (cve, cell), r in by_key.items():
            if cell != cell_x:
                continue
            r2 = by_key.get((cve, cell_y))
            if r2 is None:
                continue
            pairs.append((int(r["effective_pass_end_to_end"]), int(r2["effective_pass_end_to_end"])))
            cves_used.append(cve)
        result = mcnemar_paired(pairs)
        row = {
            "comparison_id": cmp_id,
            "cell_x": cell_x,
            "cell_y": cell_y,
            "metric": "end_to_end_pass_effective",
            "test": "McNemar exact binomial",
            "n_pairs": result["n_pairs"],
            "x_pass": sum(p[0] for p in pairs),
            "y_pass": sum(p[1] for p in pairs),
            "b_y_wins": result["b_y_wins"],
            "c_x_wins": result["c_x_wins"],
            "n_discordant": result["n_discordant"],
            "p_exact": result["p_exact"],
            "diff_rate": result["diff_rate"],
            "ci_lo": result["ci_lo"],
            "ci_hi": result["ci_hi"],
            "cves": ",".join(cves_used),
        }
        comparison_rows.append(row)
        p_values_for_fdr.append((cmp_id, result["p_exact"]))

    # BH FDR 0.1
    if p_values_for_fdr:
        rej = bh_fdr([p for _, p in p_values_for_fdr], alpha=0.1)
        for i, (cid, _) in enumerate(p_values_for_fdr):
            for row in comparison_rows:
                if row["comparison_id"] == cid:
                    row["reject_null_bh_0p1"] = bool(rej[i])
    out.extend(comparison_rows)

    # Cost / wallclock across cells (C1, C2)
    for metric, field, cid in [
        ("cost_usd_total", "total_cost_usd", "C1"),
        ("wallclock_seconds", "wallclock_seconds", "C2"),
    ]:
        values_by_cell = defaultdict(list)
        for r in enriched:
            values_by_cell[r["cell"]].append(float(r[field]))
        kw = kruskal_by_cell(dict(values_by_cell))
        out.append({
            "comparison_id": cid,
            "metric": metric,
            "test": "Kruskal-Wallis + Mann-Whitney pairwise",
            "H": kw.get("H"),
            "p_overall": kw.get("p"),
            "medians": json.dumps(kw.get("medians", {})),
            "pairwise": json.dumps(kw.get("pairwise", {})),
            "cells": ",".join(kw.get("cells", [])),
        })

    # Tool-call redundancy R1 per-cell summary
    redundancy_summary = defaultdict(list)
    for r in enriched:
        if r.get("redundancy_rate_total") is None:
            continue
        redundancy_summary[r["cell"]].append(float(r["redundancy_rate_total"]))
    for cell, vs in sorted(redundancy_summary.items()):
        out.append({
            "comparison_id": f"R1_{cell}",
            "metric": "redundancy_rate_total",
            "test": "descriptive",
            "n": len(vs),
            "mean": float(np.mean(vs)),
            "median": float(np.median(vs)),
            "min": float(np.min(vs)),
            "max": float(np.max(vs)),
        })

    # CNR R2
    cnr_summary = defaultdict(list)
    for r in enriched:
        if r.get("cnr_mean") is None:
            continue
        cnr_summary[r["cell"]].append(float(r["cnr_mean"]))
    for cell, vs in sorted(cnr_summary.items()):
        out.append({
            "comparison_id": f"R2_{cell}",
            "metric": "cnr_mean",
            "test": "descriptive",
            "n": len(vs),
            "mean": float(np.mean(vs)),
            "median": float(np.median(vs)),
            "min": float(np.min(vs)),
            "max": float(np.max(vs)),
        })
    return out


def stratified_summary(enriched: list[dict]) -> list[dict]:
    """Per-stratum per-cell breakdown for fig_stratified + narrative."""
    out = []
    by_stratum_cell = defaultdict(list)
    for r in enriched:
        by_stratum_cell[(r["stratum"], r["cell"])].append(r)
    for (stratum, cell), rs in sorted(by_stratum_cell.items()):
        n = len(rs)
        pass_cnt = sum(1 for r in rs if r["effective_pass_end_to_end"])
        out.append({
            "stratum": stratum,
            "cell": cell,
            "n": n,
            "end_to_end_pass_rate": pass_cnt / n if n else 0,
            "mean_cost_usd": float(np.mean([r["total_cost_usd"] for r in rs])) if n else 0,
            "mean_wallclock_s": float(np.mean([r["wallclock_seconds"] for r in rs])) if n else 0,
        })
    return out


def per_cve_breakdown(enriched: list[dict]) -> list[dict]:
    out = []
    for r in enriched:
        out.append({
            "cve_id": r["cve_id"],
            "cell": r["cell"],
            "replicate": r["replicate"],
            "stratum": r["stratum"],
            "project": r["project"],
            "termination": r["termination_reason"],
            "cost_usd": r["total_cost_usd"],
            "wallclock_s": r["wallclock_seconds"],
            "event_count": r["event_count"],
            "audit_violations": r["audit_violations"],
            "no_patch_produced": r.get("no_patch_produced", False),
            "mech_builder": r["mech_builder"],
            "mech_exploiter": r["mech_exploiter"],
            "mech_fixer": r["mech_fixer"],
            "mech_end_to_end": r["mech_end_to_end"],
            "corrected_end_to_end": r.get("corrected_mechanical_end_to_end"),
            "effective_end_to_end": r.get("effective_pass_end_to_end"),
            "cnr_mean": r.get("cnr_mean"),
            "tool_calls_total": r.get("tool_calls_total"),
            "redundancy_rate": r.get("redundancy_rate_total"),
        })
    return out


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    if not rows:
        logger.warning("No rows for %s; skipping", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = columns or sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    logger.info("Wrote %s (%d rows, %d cols)", path, len(rows), len(cols))


def main() -> int:
    locked = load_locked()
    index_rows = load_index()
    logger.info("Loaded INDEX.jsonl: %d rows", len(index_rows))

    # Compute per-run metrics (events.jsonl parse once per run)
    metrics_by_run: dict[str, RunMetrics] = {}
    for r in index_rows:
        run_dir = Path(r["path"])
        if not run_dir.is_absolute():
            run_dir = REPO_ROOT / run_dir
        m = compute_run_metrics(run_dir, r["cell"])
        m.run_id = r["run_id"]
        metrics_by_run[r["run_id"]] = m

    enriched = enrich_index(index_rows, locked, metrics_by_run)
    write_csv(TABLES_DIR / "index_enriched.csv", enriched)

    strat = stratified_summary(enriched)
    write_csv(TABLES_DIR / "stratified_summary.csv", strat,
              columns=["stratum", "cell", "n", "end_to_end_pass_rate",
                       "mean_cost_usd", "mean_wallclock_s"])

    cmp_rows = run_comparisons(enriched)
    write_csv(TABLES_DIR / "comparison_results.csv", cmp_rows)

    breakdown = per_cve_breakdown(enriched)
    write_csv(TABLES_DIR / "per_cve_breakdown.csv", breakdown)

    logger.info("All tables written to %s", TABLES_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
