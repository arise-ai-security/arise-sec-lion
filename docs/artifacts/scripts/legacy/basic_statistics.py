"""Basic statistics for Pillar B metrics across A1, A2, B1, B2 cells.

Two output table families:
  1. Per-category (A, B) tables: rows = metrics, columns = CVE instance IDs.
  2. Cross-cell summary: rows = metrics, columns = A1, A2, B1, B2, A_mean, B_mean.

Filters to replicate == 0 (drops A1 anchor replicates on openjpeg) so every cell
reports 10 runs on the same 10 CVEs.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "tables" / "index_enriched.csv"

METRIC_COLS: dict[str, str] = {
    "cost_usd": "total_cost_usd",
    "wallclock_s": "wallclock_seconds",
    "event_count": "event_count",
    "tool_calls": "tool_calls_total",
    "redundancy_rate": "redundancy_rate_total",
    "cnr_mean": "cnr_mean",
    "audit_violations": "audit_violations",
    "mech_builder": "mech_builder",
    "mech_exploiter": "mech_exploiter",
    "mech_fixer": "mech_fixer",
    "end_to_end": "effective_pass_end_to_end",
}

BOOL_COLS = {"mech_builder", "mech_exploiter", "mech_fixer", "end_to_end"}


def short_cve(cve: str) -> str:
    """'njs.cve-2022-32414' -> 'njs:2022-32414' for compact headers."""
    project, _, rest = cve.partition(".")
    return f"{project}:{rest.removeprefix('cve-')}"


def load() -> pd.DataFrame:
    df = pd.read_csv(INDEX)
    df = df[df["cell"].isin(["A1", "A2", "B1", "B2"]) & (df["replicate"] == 0)].copy()
    for col in METRIC_COLS.values():
        if col in BOOL_COLS:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("mech_builder", "mech_exploiter", "mech_fixer", "effective_pass_end_to_end"):
        df[col] = df[col].astype(str).str.lower().map({"true": 1, "false": 0}).astype(int)
    df["cve_short"] = df["cve_id"].map(short_cve)
    return df


def per_category_table(df: pd.DataFrame, cells: list[str], label: str) -> pd.DataFrame:
    """Rows = cell/metric pairs; columns = CVE instance IDs (wide)."""
    sub = df[df["cell"].isin(cells)].copy()
    cves = sorted(sub["cve_short"].unique())
    frames: list[pd.DataFrame] = []
    for cell in cells:
        cell_df = sub[sub["cell"] == cell].set_index("cve_short")
        rows: dict[str, pd.Series] = {}
        for label_name, col in METRIC_COLS.items():
            rows[f"{cell}.{label_name}"] = cell_df[col].reindex(cves)
        frames.append(pd.DataFrame(rows).T)
    table = pd.concat(frames)
    table.columns = [f"{c}" for c in cves]
    table.index.name = f"{label}_metric"
    return table


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    """Rows = metric + aggregate (mean/median); columns = A1 A2 B1 B2 A_mean B_mean."""
    cells = ["A1", "A2", "B1", "B2"]
    rows: list[tuple[str, str, dict[str, float]]] = []
    for label_name, col in METRIC_COLS.items():
        for agg_name, agg_fn in (("mean", "mean"), ("median", "median")):
            row_vals: dict[str, float] = {}
            for cell in cells:
                series = df.loc[df["cell"] == cell, col]
                row_vals[cell] = getattr(series, agg_fn)()
            a_vals = df.loc[df["cell"].isin(["A1", "A2"]), col]
            b_vals = df.loc[df["cell"].isin(["B1", "B2"]), col]
            row_vals["A_all"] = getattr(a_vals, agg_fn)()
            row_vals["B_all"] = getattr(b_vals, agg_fn)()
            rows.append((label_name, agg_name, row_vals))
    idx = pd.MultiIndex.from_tuples([(m, a) for m, a, _ in rows], names=["metric", "agg"])
    table = pd.DataFrame([r for _, _, r in rows], index=idx)
    return table


def fmt(val: float, metric: str) -> str:
    if pd.isna(val):
        return "—"
    if metric in {"cost_usd"}:
        return f"${val:,.2f}"
    if metric in {"wallclock_s", "event_count", "tool_calls", "audit_violations"}:
        return f"{val:,.0f}" if float(val).is_integer() else f"{val:,.1f}"
    if metric in {"redundancy_rate", "cnr_mean"}:
        return "—" if pd.isna(val) else f"{val:.3f}"
    if metric in BOOL_COLS or metric in {"mech_builder", "mech_exploiter", "mech_fixer", "end_to_end"}:
        return f"{val:.2f}" if not float(val).is_integer() else f"{int(val)}"
    return f"{val:.3f}"


def render_markdown(table: pd.DataFrame, index_label: str, metric_extractor) -> str:
    cols = list(table.columns)
    header = "| " + index_label + " | " + " | ".join(cols) + " |"
    sep = "|" + "---|" * (len(cols) + 1)
    lines = [header, sep]
    for idx, row in table.iterrows():
        metric = metric_extractor(idx)
        if isinstance(idx, tuple):
            label = f"{idx[0]} [{idx[1]}]"
        else:
            label = str(idx)
        cells_rendered = [fmt(row[c], metric) for c in cols]
        lines.append("| " + label + " | " + " | ".join(cells_rendered) + " |")
    return "\n".join(lines)


def main() -> None:
    df = load()
    assert (df.groupby("cell").size() == 10).all(), df.groupby("cell").size().to_dict()

    a_table = per_category_table(df, ["A1", "A2"], "A")
    b_table = per_category_table(df, ["B1", "B2"], "B")
    s_table = summary_table(df)

    def a_metric(idx: str) -> str:
        return idx.split(".", 1)[1]

    def b_metric(idx: str) -> str:
        return idx.split(".", 1)[1]

    print("## Table 1A — Category A (A1, A2): metrics × CVE\n")
    print(render_markdown(a_table, "cell.metric", a_metric))
    print("\n## Table 1B — Category B (B1, B2): metrics × CVE\n")
    print(render_markdown(b_table, "cell.metric", b_metric))
    print("\n## Table 2 — Summary: metrics × cell (+ A/B aggregates)\n")
    print(render_markdown(s_table, "metric [agg]", lambda idx: idx[0]))


if __name__ == "__main__":
    main()
