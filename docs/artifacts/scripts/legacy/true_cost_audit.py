"""Audit reported vs TRUE per-run cost.

INDEX.jsonl's `total_cost_usd` is computed by `_sum_cost` in
`experiments/run_experiment.py`, which only sums `tokens_consumed` events.
Tree cells additionally emit `worker_cost_recorded` events (one per spawned
Claude Code worker) that INDEX never reads. This script reconstructs the true
cost by summing both event types, then compares against INDEX.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "dataset" / "runs"


def scan_run(run_dir: Path) -> dict:
    events = run_dir / "events.jsonl"
    agg = defaultdict(float)
    if not events.exists():
        return agg
    with events.open() as f:
        for line in f:
            e = json.loads(line)
            pl = e.get("payload") or {}
            et = e.get("event_type")
            if et == "tokens_consumed":
                agg["tc_cost"] += pl.get("cost_usd") or 0
                agg["tc_in"] += pl.get("input_tokens") or 0
                agg["tc_out"] += pl.get("output_tokens") or 0
                agg["tc_cache_r"] += pl.get("cache_read_input_tokens") or 0
                agg["tc_cache_c"] += pl.get("cache_creation_input_tokens") or 0
            elif et == "worker_cost_recorded":
                agg["wc_cost"] += pl.get("cost_usd") or 0
                agg["wc_in"] += pl.get("prompt_tokens") or 0
                agg["wc_out"] += pl.get("completion_tokens") or 0
                agg["wc_cache_r"] += pl.get("cache_read_tokens") or 0
                agg["wc_cache_c"] += pl.get("cache_write_tokens") or 0
                agg["wc_dur_s"] += pl.get("duration_seconds") or 0
                agg["wc_n"] += 1
    return agg


def main() -> None:
    rows: list[dict] = []
    for cve_dir in sorted(RUNS.iterdir()):
        for cell in ("A1", "A2", "B1", "B2"):
            run_dir = cve_dir / cell / "0"
            if not run_dir.exists():
                continue
            a = scan_run(run_dir)
            rows.append(
                {
                    "cve": cve_dir.name,
                    "cell": cell,
                    "index_cost": a["tc_cost"],
                    "worker_cost_hidden": a["wc_cost"],
                    "true_cost": a["tc_cost"] + a["wc_cost"],
                    "n_workers": int(a["wc_n"]),
                    "worker_dur_s": a["wc_dur_s"],
                    "total_output_tok": a["tc_out"] + a["wc_out"],
                    "total_cache_read_tok": a["tc_cache_r"] + a["wc_cache_r"],
                    "total_cache_create_tok": a["tc_cache_c"] + a["wc_cache_c"],
                    "total_raw_input_tok": a["tc_in"] + a["wc_in"],
                }
            )
    df = pd.DataFrame(rows)

    def fmt_money(x: float) -> str:
        return f"${x:,.2f}"

    def fmt_tok(x: float) -> str:
        return f"{x/1e6:.2f}M" if x > 1e5 else f"{x:,.0f}"

    print("## Per-run TRUE cost vs INDEX cost\n")
    print("| cve | cell | INDEX.cost | worker_hidden | **TRUE** | workers | out_tok | cache_read |")
    print("|---|---|---|---|---|---|---|---|")
    for _, r in df.iterrows():
        print(
            f"| {r['cve']} | {r['cell']} | {fmt_money(r['index_cost'])} | "
            f"{fmt_money(r['worker_cost_hidden'])} | **{fmt_money(r['true_cost'])}** | "
            f"{r['n_workers']} | {fmt_tok(r['total_output_tok'])} | "
            f"{fmt_tok(r['total_cache_read_tok'])} |"
        )

    print("\n## Per-cell aggregates (N=10 each)\n")
    g = df.groupby("cell").agg(
        index_mean=("index_cost", "mean"),
        index_median=("index_cost", "median"),
        worker_hidden_mean=("worker_cost_hidden", "mean"),
        TRUE_mean=("true_cost", "mean"),
        TRUE_median=("true_cost", "median"),
        out_tok_mean=("total_output_tok", "mean"),
        cache_read_mean=("total_cache_read_tok", "mean"),
        workers_mean=("n_workers", "mean"),
    )
    print("| cell | INDEX[mean] | INDEX[med] | worker_hidden[mean] | **TRUE[mean]** | TRUE[med] | out_tok[mean] | cache_read[mean] | workers[mean] |")
    print("|---|---|---|---|---|---|---|---|---|")
    for cell, row in g.iterrows():
        print(
            f"| {cell} | {fmt_money(row['index_mean'])} | {fmt_money(row['index_median'])} | "
            f"{fmt_money(row['worker_hidden_mean'])} | **{fmt_money(row['TRUE_mean'])}** | "
            f"{fmt_money(row['TRUE_median'])} | {fmt_tok(row['out_tok_mean'])} | "
            f"{fmt_tok(row['cache_read_mean'])} | {row['workers_mean']:.1f} |"
        )


if __name__ == "__main__":
    main()
