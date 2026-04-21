"""Pillar B v2 analysis: compare A1/A2 (v1) vs B1/B2 (v2) on matched CVE set.

Data sources
------------
A1, A2 runs come from the v1 dataset at ``dataset/`` (unchanged since the
original Pillar A / B collection). Only replicate 0 is used so A1 matches
the N=10 shape of A2 / B1 / B2.

B1, B2 runs come from the v2 dataset at ``dataset-v2-20260420/``, produced
after the JSON-retry fix, deeper topology, Sonnet manager, and Claude SDK
system_prompt pin. Includes 9 anomaly-flagged runs (the rows still count
toward N; ``anomaly_kinds`` is kept as a separate column).

Output
------
Three markdown tables printed to stdout, matching the shape of the earlier
``basic_statistics.py`` request:

* **Table 1A** -- A1/A2 × 10 CVE columns.
* **Table 1B** -- B1/B2 × 10 CVE columns.
* **Table 2** -- per-cell summary (mean + median) across A1, A2, B1, B2,
  with A_all / B_all pooled aggregates.

Followed by:

* A short "v2 deltas" section showing v1 → v2 changes on B cells (MANAGER
  layer appearance, true-cost correction).
* Kruskal-Wallis on ``total_cost_usd`` and ``wallclock_seconds`` across the
  four cells, with pairwise Mann-Whitney U.
* McNemar exact on A1 vs B2 ``end_to_end_pass``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
from scipy import stats  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[3]
V1_INDEX = ROOT / "dataset" / "INDEX.jsonl"
V1_RUNS = ROOT / "dataset" / "runs"
V2_INDEX = ROOT / "dataset-v2-20260420" / "INDEX.jsonl"
V2_RUNS = ROOT / "dataset-v2-20260420" / "runs"

PRIMARY_CVES: tuple[str, ...] = (
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


def _short_cve(cve: str) -> str:
    project, _, rest = cve.partition(".")
    return f"{project}:{rest.removeprefix('cve-')}"


def load_index(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return pd.DataFrame(rows)


def _count_agents(events_path: Path) -> tuple[int, int, int]:
    """Return (boss, manager, worker) agent_created counts."""
    boss = mgr = wrk = 0
    if not events_path.exists():
        return (0, 0, 0)
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event_type") != "agent_created":
            continue
        role = e.get("role")
        if role == "BOSS":
            boss += 1
        elif role == "MANAGER":
            mgr += 1
        elif role == "WORKER":
            wrk += 1
    return (boss, mgr, wrk)


def _count_tool_calls(events_path: Path) -> int:
    if not events_path.exists():
        return 0
    n = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event_type") == "tool_use":
            n += 1
    return n


def _read_anomaly_kinds(run_dir: Path) -> list[str]:
    ap = run_dir / "anomaly.json"
    if not ap.exists():
        return []
    try:
        data = json.loads(ap.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    kinds: list[str] = []
    for a in data:
        if isinstance(a, dict) and isinstance(a.get("kind"), str):
            kinds.append(a["kind"])
    return kinds


def _compute_cost_breakdown(events_path: Path) -> tuple[float, float, float]:
    """Return (tokens_consumed_usd, worker_recorded_usd, total_usd).

    For A cells only -- B cells already ship cost_breakdown in the INDEX.
    """
    tc = wc = 0.0
    if not events_path.exists():
        return (0.0, 0.0, 0.0)
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = e.get("payload") or {}
        cost = payload.get("cost_usd")
        if cost is None:
            continue
        et = e.get("event_type")
        if et == "tokens_consumed":
            tc += float(cost)
        elif et == "worker_cost_recorded":
            wc += float(cost)
    return (tc, wc, tc + wc)


def build_runs_table() -> pd.DataFrame:
    """Load A1/A2 from v1 + B1/B2 from v2 into a single per-run DataFrame.

    Filters to primary 10 CVEs × replicate=0 for every cell so each cell has
    N=10 on a matched CVE set.
    """
    rows: list[dict[str, Any]] = []

    v1_df = load_index(V1_INDEX)
    v1_df = v1_df[
        v1_df["cell"].isin(["A1", "A2"])
        & (v1_df["replicate"] == 0)
        & v1_df["cve_id"].isin(PRIMARY_CVES)
    ].copy()
    for _, r in v1_df.iterrows():
        run_dir = V1_RUNS / str(r["cve_id"]) / str(r["cell"]) / str(int(r["replicate"]))
        events_path = run_dir / "events.jsonl"
        boss, mgr, wrk = _count_agents(events_path)
        tool_calls = _count_tool_calls(events_path)
        tc, wcost, total = _compute_cost_breakdown(events_path)
        mech = r.get("mechanical_pass") or {}
        rows.append(
            {
                "source": "v1",
                "cve_id": r["cve_id"],
                "cell": r["cell"],
                "replicate": int(r["replicate"]),
                "total_cost_usd": float(r.get("total_cost_usd") or 0.0),
                "tokens_consumed_usd": tc,
                "worker_recorded_usd": wcost,
                "cost_breakdown_total_usd": total,
                "wallclock_seconds": float(r.get("wallclock_seconds") or 0.0),
                "event_count": int(r.get("event_count") or 0),
                "tool_calls_total": tool_calls,
                "audit_violations": int(r.get("audit_violations") or 0),
                "builder_pass": bool(mech.get("builder")),
                "exploiter_pass": bool(mech.get("exploiter")),
                "fixer_pass": bool(mech.get("fixer")),
                "end_to_end_pass": bool(mech.get("end_to_end")),
                "agents_boss": boss,
                "agents_manager": mgr,
                "agents_worker": wrk,
                "agents_total": boss + mgr + wrk,
                "anomaly_kinds": _read_anomaly_kinds(run_dir),
            }
        )

    v2_df = load_index(V2_INDEX)
    v2_df = v2_df[
        v2_df["cell"].isin(["B1", "B2"])
        & (v2_df["replicate"] == 0)
        & v2_df["cve_id"].isin(PRIMARY_CVES)
    ].copy()
    for _, r in v2_df.iterrows():
        run_dir = V2_RUNS / str(r["cve_id"]) / str(r["cell"]) / str(int(r["replicate"]))
        events_path = run_dir / "events.jsonl"
        boss, mgr, wrk = _count_agents(events_path)
        tool_calls = _count_tool_calls(events_path)
        cb = r.get("cost_breakdown") or {}
        if cb:
            tc = float(cb.get("tokens_consumed_usd") or 0.0)
            wcost = float(cb.get("worker_recorded_usd") or 0.0)
            total = float(cb.get("total_usd") or 0.0)
        else:
            tc, wcost, total = _compute_cost_breakdown(events_path)
        mech = r.get("mechanical_pass") or {}
        rows.append(
            {
                "source": "v2",
                "cve_id": r["cve_id"],
                "cell": r["cell"],
                "replicate": int(r["replicate"]),
                "total_cost_usd": float(r.get("total_cost_usd") or 0.0),
                "tokens_consumed_usd": tc,
                "worker_recorded_usd": wcost,
                "cost_breakdown_total_usd": total,
                "wallclock_seconds": float(r.get("wallclock_seconds") or 0.0),
                "event_count": int(r.get("event_count") or 0),
                "tool_calls_total": tool_calls,
                "audit_violations": int(r.get("audit_violations") or 0),
                "builder_pass": bool(mech.get("builder")),
                "exploiter_pass": bool(mech.get("exploiter")),
                "fixer_pass": bool(mech.get("fixer")),
                "end_to_end_pass": bool(mech.get("end_to_end")),
                "agents_boss": boss,
                "agents_manager": mgr,
                "agents_worker": wrk,
                "agents_total": boss + mgr + wrk,
                "anomaly_kinds": _read_anomaly_kinds(run_dir),
            }
        )

    df = pd.DataFrame(rows)
    # INDEX.jsonl is append-only; when a row is re-executed (e.g. after an
    # anomaly halt + resume) the later entry supersedes the earlier one.
    # Keep the last occurrence of each (cell, cve_id, replicate) tuple so
    # per-CVE pivots stay single-valued.
    before = len(df)
    df = df.drop_duplicates(
        subset=["source", "cell", "cve_id", "replicate"], keep="last"
    ).reset_index(drop=True)
    if len(df) != before:
        print(f"note: dropped {before - len(df)} duplicate INDEX row(s); kept 'last'")
    df["cve_short"] = df["cve_id"].map(_short_cve)
    return df


METRICS: dict[str, str] = {
    "cost_usd": "cost_breakdown_total_usd",
    "wallclock_s": "wallclock_seconds",
    "event_count": "event_count",
    "tool_calls": "tool_calls_total",
    "audit_violations": "audit_violations",
    "agents_total": "agents_total",
    "agents_manager": "agents_manager",
    "builder_pass": "builder_pass",
    "exploiter_pass": "exploiter_pass",
    "fixer_pass": "fixer_pass",
    "end_to_end": "end_to_end_pass",
}


def _fmt(val: Any, metric: str) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    if metric == "cost_usd":
        return f"${float(val):,.2f}"
    if metric in {"wallclock_s", "event_count", "tool_calls", "audit_violations", "agents_total", "agents_manager"}:
        return f"{float(val):,.0f}" if float(val).is_integer() else f"{float(val):,.1f}"
    if metric in {"builder_pass", "exploiter_pass", "fixer_pass", "end_to_end"}:
        # Per-CVE cells are booleans (0 or 1). Summary aggregates (mean across
        # 10 runs) are fractional. Render integers as 0/1 and fractions as
        # 2-decimal so "builder_pass [mean] = 0.20" stays readable.
        try:
            f = float(val)
        except (TypeError, ValueError):
            return "—"
        return f"{int(f)}" if f.is_integer() else f"{f:.2f}"
    return f"{val}"


def per_category_table(df: pd.DataFrame, cells: list[str]) -> pd.DataFrame:
    sub = df[df["cell"].isin(cells)].copy()
    cves_short = sorted(sub["cve_short"].unique())
    frames: list[pd.DataFrame] = []
    for cell in cells:
        cell_df = sub[sub["cell"] == cell].set_index("cve_short")
        row_data: dict[str, pd.Series] = {}
        for label, col in METRICS.items():
            row_data[f"{cell}.{label}"] = cell_df[col].reindex(cves_short)
        frames.append(pd.DataFrame(row_data).T)
    table = pd.concat(frames)
    table.columns = [c for c in cves_short]
    return table


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    cells = ["A1", "A2", "B1", "B2"]
    rows: list[tuple[str, str, dict[str, float]]] = []
    for label, col in METRICS.items():
        for agg in ("mean", "median"):
            vals: dict[str, float] = {}
            for cell in cells:
                series = df.loc[df["cell"] == cell, col]
                if col.endswith("_pass"):
                    series = series.astype(float)
                vals[cell] = getattr(series, agg)()
            a_pool = df.loc[df["cell"].isin(["A1", "A2"]), col]
            b_pool = df.loc[df["cell"].isin(["B1", "B2"]), col]
            if col.endswith("_pass"):
                a_pool = a_pool.astype(float)
                b_pool = b_pool.astype(float)
            vals["A_all"] = getattr(a_pool, agg)()
            vals["B_all"] = getattr(b_pool, agg)()
            rows.append((label, agg, vals))
    idx = pd.MultiIndex.from_tuples([(m, a) for m, a, _ in rows], names=["metric", "agg"])
    return pd.DataFrame([r for _, _, r in rows], index=idx)


def render_markdown(table: pd.DataFrame, index_label: str, metric_extractor) -> str:
    cols = list(table.columns)
    header = "| " + index_label + " | " + " | ".join(str(c) for c in cols) + " |"
    sep = "|" + "---|" * (len(cols) + 1)
    lines = [header, sep]
    for idx, row in table.iterrows():
        metric = metric_extractor(idx)
        label = f"{idx[0]} [{idx[1]}]" if isinstance(idx, tuple) else str(idx)
        cells_rendered = [_fmt(row[c], metric) for c in cols]
        lines.append("| " + label + " | " + " | ".join(cells_rendered) + " |")
    return "\n".join(lines)


def v2_deltas(df: pd.DataFrame) -> list[str]:
    """Short bulleted list of v1→v2 changes on B cells.

    v1 numbers are published in ``docs/pillar_b/2026-04-19-pillar-b-analysis.md``;
    we keep those as constants rather than re-reading v1 dataset/runs/B*/
    (which still has the obsolete v1 data).
    """
    V1_B1_MEAN_COST = 0.44  # INDEX-reported (missing worker cost)
    V1_B2_MEAN_COST = 0.58
    V1_B1_TRUE_COST = 12.84  # true with worker_cost_recorded
    V1_B2_TRUE_COST = 0.58  # (all crashed at decompose, no workers ran)
    V1_B2_RUNS_WITH_MANAGER = 0
    V1_B2_TOTAL_AGENTS_MEAN = 1.0

    v2_b1 = df[df["cell"] == "B1"]
    v2_b2 = df[df["cell"] == "B2"]
    b2_with_mgr = int((v2_b2["agents_manager"] >= 1).sum())
    out = []
    out.append(
        f"- **B2 managers spawned**: v1 {V1_B2_RUNS_WITH_MANAGER}/10 → v2 {b2_with_mgr}/{len(v2_b2)}"
    )
    out.append(
        f"- **B2 mean total agents**: v1 {V1_B2_TOTAL_AGENTS_MEAN:.1f} → v2 {v2_b2['agents_total'].mean():.1f}"
    )
    out.append(
        f"- **B1 mean true cost**: v1 ${V1_B1_TRUE_COST:.2f} → v2 ${v2_b1['cost_breakdown_total_usd'].mean():.2f} "
        f"(v1 INDEX reported ${V1_B1_MEAN_COST:.2f})"
    )
    out.append(
        f"- **B2 mean cost**: v1 ${V1_B2_TRUE_COST:.2f} → v2 ${v2_b2['cost_breakdown_total_usd'].mean():.2f} "
        "(v1 all crashed at decompose; v2 actually ran)"
    )
    b_rows = df[df["cell"].isin(["B1", "B2"])]
    n_anom = int(b_rows["anomaly_kinds"].apply(bool).sum())
    anom_kinds = Counter(k for kinds in b_rows["anomaly_kinds"] for k in kinds)
    out.append(f"- **v2 B-cell anomaly rate**: {n_anom}/{len(v2_b1) + len(v2_b2)} runs; kinds: {dict(anom_kinds)}")
    return out


def stats_tests(df: pd.DataFrame) -> list[str]:
    """Cross-cell unpaired (Kruskal-Wallis) + paired A1/B2 (Wilcoxon signed-rank)
    + McNemar A1 vs B2 on end_to_end_pass.
    """
    out = []
    out.append("**Exploratory unpaired (ignores same-CVE pairing):**")
    groups = {
        cell: df.loc[df["cell"] == cell, "cost_breakdown_total_usd"].dropna().to_numpy()
        for cell in ("A1", "A2", "B1", "B2")
    }
    if all(len(g) > 0 for g in groups.values()):
        h, p = stats.kruskal(*groups.values())
        out.append(f"- **C1 cost** (KW across A1/A2/B1/B2): H={h:.2f}, p={p:.2e}")

    wall = {
        cell: df.loc[df["cell"] == cell, "wallclock_seconds"].dropna().to_numpy()
        for cell in ("A1", "A2", "B1", "B2")
    }
    if all(len(w) > 0 for w in wall.values()):
        h, p = stats.kruskal(*wall.values())
        out.append(f"- **C2 wallclock** (KW across A1/A2/B1/B2): H={h:.2f}, p={p:.2e}")

    out.append("")
    out.append("**Paired on same CVE (A1 vs B2, Wilcoxon signed-rank):**")
    for metric_label, col in (("cost", "cost_breakdown_total_usd"), ("wallclock_s", "wallclock_seconds")):
        paired = (
            df[df["cell"].isin(["A1", "B2"])]
            .pivot_table(index="cve_id", columns="cell", values=col, aggfunc="first")
            .dropna()
        )
        if {"A1", "B2"}.issubset(paired.columns) and len(paired) >= 2:
            diffs = paired["A1"] - paired["B2"]
            # Wilcoxon requires at least one non-zero difference; guard the
            # degenerate all-equal case.
            if (diffs != 0).any():
                res = stats.wilcoxon(paired["A1"], paired["B2"])
                out.append(
                    f"- **{metric_label}** (N_paired={len(paired)}): "
                    f"W={res.statistic:.2f}, p={res.pvalue:.3f}, "
                    f"median(A1-B2)={diffs.median():.2f}"
                )
            else:
                out.append(f"- **{metric_label}** (N_paired={len(paired)}): all paired diffs are zero")

    # H1: McNemar A1 vs B2 paired on end_to_end_pass
    merged = (
        df[df["cell"].isin(["A1", "B2"])]
        .pivot_table(index="cve_id", columns="cell", values="end_to_end_pass", aggfunc="first")
        .dropna()
    )
    if {"A1", "B2"}.issubset(merged.columns):
        a_only = int(((merged["A1"] == True) & (merged["B2"] == False)).sum())  # noqa: E712
        b_only = int(((merged["A1"] == False) & (merged["B2"] == True)).sum())  # noqa: E712
        both = int(((merged["A1"] == True) & (merged["B2"] == True)).sum())  # noqa: E712
        neither = int(((merged["A1"] == False) & (merged["B2"] == False)).sum())  # noqa: E712
        total = a_only + b_only + both + neither
        # scipy.stats.binomtest replaces deprecated binom_test
        if a_only + b_only > 0:
            res = stats.binomtest(a_only, a_only + b_only, p=0.5)
            p = res.pvalue
        else:
            p = 1.0
        out.append(
            f"- **H1 A1 vs B2** (McNemar exact on end_to_end_pass): "
            f"N={total} paired CVEs, both-pass={both}, neither-pass={neither}, "
            f"A1-only={a_only}, B2-only={b_only}, p={p:.3f}"
        )
    return out


def main() -> None:
    df = build_runs_table()

    print(f"Loaded N={len(df)} runs  ({', '.join(str(s) for s in df['cell'].value_counts().items())})")
    print()

    a_table = per_category_table(df, ["A1", "A2"])
    b_table = per_category_table(df, ["B1", "B2"])
    s_table = summary_table(df)

    def a_metric(idx: str) -> str:
        return idx.split(".", 1)[1]

    def b_metric(idx: str) -> str:
        return idx.split(".", 1)[1]

    print("## Table 1A — Category A (A1, A2): metrics × CVE\n")
    print(render_markdown(a_table, "cell.metric", a_metric))
    print()
    print("## Table 1B — Category B (B1, B2): metrics × CVE\n")
    print(render_markdown(b_table, "cell.metric", b_metric))
    print()
    print("## Table 2 — Per-cell summary (metrics × cell + pooled A/B)\n")
    print(render_markdown(s_table, "metric [agg]", lambda idx: idx[0]))
    print()
    print("## v1 → v2 deltas (B cells)\n")
    for line in v2_deltas(df):
        print(line)
    print()
    print("## Paired statistical tests\n")
    for line in stats_tests(df):
        print(line)


if __name__ == "__main__":
    main()
