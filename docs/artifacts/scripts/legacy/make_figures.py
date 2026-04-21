"""Generate the 7 required Pillar B figures at 300 dpi.

Reads docs/pillar_b/tables/index_enriched.csv and emits:
- fig_h1_endtoend.png
- fig_cost_by_cell.png
- fig_wallclock_by_cell.png
- fig_stratified.png
- fig_cost_vs_pass.png
- fig_cnr_distribution.png
- fig_toolcall_redundancy.png
"""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({
    "figure.dpi": 120,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "font.size": 10,
})

REPO_ROOT = Path(__file__).resolve().parents[3]
TABLES = REPO_ROOT / "docs" / "pillar_b" / "tables"
FIGS = REPO_ROOT / "docs" / "pillar_b" / "figures"
FIGS.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("figures")

CELL_ORDER = ["A1", "A2", "A3", "A4", "B1", "B2"]
CELL_COLORS = {
    "A1": "#3A77B8",
    "A2": "#5391C3",
    "A3": "#7BA9CC",
    "A4": "#9EC0D7",
    "B1": "#E0812A",
    "B2": "#F1A652",
}


def load_enriched() -> list[dict]:
    rows = []
    with (TABLES / "index_enriched.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            for k in ("total_cost_usd", "wallclock_seconds", "cnr_mean",
                      "redundancy_rate_total"):
                if r.get(k):
                    try:
                        r[k] = float(r[k])
                    except ValueError:
                        r[k] = None
                else:
                    r[k] = None
            for k in ("mech_builder", "mech_exploiter", "mech_fixer", "mech_end_to_end",
                      "workspace_has_patch", "no_patch_produced",
                      "effective_pass_end_to_end",
                      "corrected_mechanical_end_to_end", "security_tool_adoption"):
                if r.get(k) in {"True", "1"}:
                    r[k] = True
                elif r.get(k) in {"False", "0"}:
                    r[k] = False
                else:
                    r[k] = None
            rows.append(r)
    return rows


def group_by_cell(rows, field, as_float: bool = True):
    out = {c: [] for c in CELL_ORDER}
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        if as_float:
            out[r["cell"]].append(float(v))
        else:
            out[r["cell"]].append(v)
    return out


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return p, max(centre - half, 0.0), min(centre + half, 1.0)


# --- figures -----------------------------------------------------------


def fig_h1_endtoend(rows):
    fig, ax = plt.subplots(figsize=(7, 4))
    xs, heights, lows, highs, ns = [], [], [], [], []
    for cell in CELL_ORDER:
        cell_rows = [r for r in rows if r["cell"] == cell]
        n = len(cell_rows)
        k = sum(1 for r in cell_rows if r["effective_pass_end_to_end"])
        p, lo, hi = wilson_ci(k, n)
        xs.append(cell)
        heights.append(p)
        lows.append(p - lo)
        highs.append(hi - p)
        ns.append(n)
    bars = ax.bar(xs, heights, yerr=[lows, highs], capsize=4,
                  color=[CELL_COLORS[c] for c in xs], edgecolor="black")
    for b, n, k in zip(bars, ns, [int(h * n) for h, n in zip(heights, ns)]):
        ax.text(b.get_x() + b.get_width() / 2, 0.02,
                f"{k}/{n}", ha="center", va="bottom", fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("end-to-end pass rate")
    ax.set_title("H1: end-to-end mechanical pass by cell (Wilson 95% CI, retrofit applied)")
    ax.set_xlabel("cell")
    fig.savefig(FIGS / "fig_h1_endtoend.png")
    plt.close(fig)
    logger.info("Wrote fig_h1_endtoend.png")


def _box(rows, field, title, ylabel, out_name, log_y=False):
    fig, ax = plt.subplots(figsize=(7, 4))
    data = group_by_cell(rows, field)
    kept = [(c, v) for c, v in data.items() if v]
    ax.boxplot([v for _, v in kept], tick_labels=[c for c, _ in kept],
               patch_artist=True,
               boxprops={"facecolor": "#eee"}, showmeans=True,
               meanprops={"marker": "D", "markerfacecolor": "black",
                          "markeredgecolor": "black", "markersize": 5})
    for i, (c, v) in enumerate(kept, start=1):
        ax.scatter(np.full(len(v), i) + np.random.uniform(-0.1, 0.1, len(v)),
                   v, color=CELL_COLORS[c], edgecolor="black", alpha=0.8, s=20)
    if log_y:
        ax.set_yscale("log")
    ax.set_title(title)
    ax.set_xlabel("cell")
    ax.set_ylabel(ylabel)
    fig.savefig(FIGS / out_name)
    plt.close(fig)
    logger.info("Wrote %s", out_name)


def fig_cost_by_cell(rows):
    _box(rows, "total_cost_usd",
         "C1: total cost (USD) per run by cell",
         "cost (USD, log scale)",
         "fig_cost_by_cell.png", log_y=True)


def fig_wallclock_by_cell(rows):
    _box(rows, "wallclock_seconds",
         "C2: wall-clock seconds per run by cell",
         "wall-clock (s, log scale)",
         "fig_wallclock_by_cell.png", log_y=True)


def fig_stratified(rows):
    strata_order = ["EASY", "MEDIUM", "HARD"]
    cells = CELL_ORDER
    fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=True)
    for ax, stratum in zip(axes, strata_order):
        xs, heights, ns = [], [], []
        for cell in cells:
            cell_rows = [r for r in rows if r["cell"] == cell and r["stratum"] == stratum]
            n = len(cell_rows)
            k = sum(1 for r in cell_rows if r["effective_pass_end_to_end"])
            xs.append(cell)
            heights.append((k / n) if n else 0.0)
            ns.append(n)
        bars = ax.bar(xs, heights, color=[CELL_COLORS[c] for c in xs], edgecolor="black")
        for b, n, h in zip(bars, ns, heights):
            k = int(h * n)
            ax.text(b.get_x() + b.get_width() / 2, 0.02,
                    f"{k}/{n}", ha="center", va="bottom", fontsize=8)
        ax.set_title(f"stratum = {stratum}")
        ax.set_ylim(0, 1.05)
        ax.set_xlabel("cell")
    axes[0].set_ylabel("end-to-end pass rate")
    fig.suptitle("Stratified end-to-end pass rate (EASY / MEDIUM / HARD × cell)")
    fig.savefig(FIGS / "fig_stratified.png")
    plt.close(fig)
    logger.info("Wrote fig_stratified.png")


def fig_cost_vs_pass(rows):
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for cell in CELL_ORDER:
        cell_rows = [r for r in rows if r["cell"] == cell]
        if not cell_rows:
            continue
        xs = [r["total_cost_usd"] for r in cell_rows]
        ys = [int(bool(r["effective_pass_end_to_end"])) + np.random.uniform(-0.04, 0.04)
              for r in cell_rows]
        ax.scatter(xs, ys, color=CELL_COLORS[cell], label=cell,
                   edgecolor="black", alpha=0.85, s=55)
    ax.set_xscale("log")
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["fail", "pass"])
    ax.set_ylim(-0.3, 1.3)
    ax.set_xlabel("cost (USD, log scale)")
    ax.set_ylabel("end-to-end outcome (jittered)")
    ax.set_title("Cost vs. end-to-end outcome by cell")
    ax.legend(ncol=6, loc="upper center", bbox_to_anchor=(0.5, -0.15))
    fig.savefig(FIGS / "fig_cost_vs_pass.png")
    plt.close(fig)
    logger.info("Wrote fig_cost_vs_pass.png")


def fig_cnr_distribution(rows):
    fig, ax = plt.subplots(figsize=(7, 4))
    data = group_by_cell(rows, "cnr_mean")
    cells_with_data = [(c, v) for c, v in data.items() if v]
    if not cells_with_data:
        ax.text(0.5, 0.5, "No CNR data (A-cell runs lack prompt_sent events)",
                ha="center", va="center", transform=ax.transAxes)
    else:
        parts = ax.violinplot([v for _, v in cells_with_data],
                              positions=range(1, len(cells_with_data) + 1),
                              showmeans=True, showmedians=True)
        for i, (c, _) in enumerate(cells_with_data):
            parts["bodies"][i].set_facecolor(CELL_COLORS[c])
            parts["bodies"][i].set_alpha(0.7)
        ax.set_xticks(range(1, len(cells_with_data) + 1))
        ax.set_xticklabels([c for c, _ in cells_with_data])
    ax.set_title("R2: Context Novelty Ratio (CNR) — tree cells only (A cells lack prompt capture)")
    ax.set_ylabel("CNR (novel tokens / prompt tokens)")
    ax.set_ylim(-0.05, 1.1)
    fig.savefig(FIGS / "fig_cnr_distribution.png")
    plt.close(fig)
    logger.info("Wrote fig_cnr_distribution.png")


def fig_toolcall_redundancy(rows):
    fig, ax = plt.subplots(figsize=(7, 4))
    means, cells_used, sample_n = [], [], []
    for c in CELL_ORDER:
        vs = [r["redundancy_rate_total"] for r in rows
              if r["cell"] == c and r["redundancy_rate_total"] is not None]
        if not vs:
            continue
        means.append(np.mean(vs))
        cells_used.append(c)
        sample_n.append(len(vs))
    if not cells_used:
        ax.text(0.5, 0.5, "No redundancy data", ha="center", va="center")
    else:
        bars = ax.bar(cells_used, means,
                      color=[CELL_COLORS[c] for c in cells_used], edgecolor="black")
        for b, m, n in zip(bars, means, sample_n):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.01,
                    f"mean={m:.2f}\nN={n}", ha="center", va="bottom", fontsize=8)
    ax.set_title("R1: tool-call redundancy rate per cell")
    ax.set_ylabel("redundancy rate (redundant / total tool calls)")
    ax.set_ylim(0, 0.6)
    # Add annotation explaining B-cell 0
    ax.annotate(
        "B-cell tool_name/tool_input were not captured\n"
        "by the SDK adapter → redundancy = 0 by construction",
        xy=(4.4, 0.55), fontsize=8, color="darkred",
        bbox={"boxstyle": "round", "fc": "mistyrose"})
    fig.savefig(FIGS / "fig_toolcall_redundancy.png")
    plt.close(fig)
    logger.info("Wrote fig_toolcall_redundancy.png")


def main() -> int:
    np.random.seed(42)
    rows = load_enriched()
    logger.info("Loaded %d enriched rows", len(rows))

    fig_h1_endtoend(rows)
    fig_cost_by_cell(rows)
    fig_wallclock_by_cell(rows)
    fig_stratified(rows)
    fig_cost_vs_pass(rows)
    fig_cnr_distribution(rows)
    fig_toolcall_redundancy(rows)
    logger.info("All figures written to %s", FIGS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
