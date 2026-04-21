"""Produce the Section 4 results table for ``docs/report-0420.md``.

Columns
-------
A1, A2, B1, B2, ``A avg`` (pooled over A1 + A2), ``B avg`` (pooled over B1 + B2).

Rows
----
Each scalar metric defined in Section 3 of ``report-0420.md``. Metrics that
cannot be summarised as a single scalar (e.g. ``tool_calls_by_tool`` map,
``artifacts_produced`` list, ``termination_reason`` enum) are skipped; in
those cases we include the most useful derived scalar (e.g. fraction of runs
with ``termination_reason == completed``) and say so in the row label.

Data sources
------------
A1, A2 come from the v1 dataset (``dataset/INDEX.jsonl``) at replicate 0,
matching the published Pillar A runs. B1, B2 come from the v2 dataset
(``dataset-v2-20260420/INDEX.jsonl``) at replicate 0. Both are restricted to
the ``PRIMARY_CVES`` set (10 CVEs) so every cell has N=10.

Per-run metrics are produced by reusing ``compute_metrics.compute_run_metrics``
for CNR, tool-call redundancy, tool-call counts, and security-tool use.
Token and cache breakdowns are aggregated from ``tokens_consumed`` and
``worker_cost_recorded`` events directly here (since those are not exposed
by ``RunMetrics``).

Output
------
Prints one GitHub-flavoured markdown table to stdout, ready to paste into
``report-0420.md`` as Section 4.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "docs" / "pillar_b" / "scripts"))
from compute_metrics import compute_run_metrics  # noqa: E402

V1_INDEX = REPO_ROOT / "dataset" / "INDEX.jsonl"
V2_INDEX = REPO_ROOT / "dataset-v2-20260420" / "INDEX.jsonl"

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

A_CELLS = ("A1", "A2")
B_CELLS = ("B1", "B2")


def _load_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _sum_tokens(events_path: Path) -> dict[str, int]:
    """Aggregate input/output/cache tokens from events.jsonl.

    ``tokens_consumed`` payload keys: ``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``.
    ``worker_cost_recorded`` payload keys: ``prompt_tokens``,
    ``completion_tokens``, ``cache_read_tokens``, ``cache_write_tokens``.
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    if not events_path.exists():
        return totals
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = e.get("event_type")
        p = e.get("payload") or {}
        if et == "tokens_consumed":
            totals["input_tokens"] += int(p.get("input_tokens") or 0)
            totals["output_tokens"] += int(p.get("output_tokens") or 0)
            totals["cache_read_tokens"] += int(p.get("cache_read_input_tokens") or 0)
            totals["cache_creation_tokens"] += int(p.get("cache_creation_input_tokens") or 0)
        elif et == "worker_cost_recorded":
            totals["input_tokens"] += int(p.get("prompt_tokens") or 0)
            totals["output_tokens"] += int(p.get("completion_tokens") or 0)
            totals["cache_read_tokens"] += int(p.get("cache_read_tokens") or 0)
            totals["cache_creation_tokens"] += int(p.get("cache_write_tokens") or 0)
    return totals


def _true_cost(row: dict[str, Any]) -> float:
    """Return honest cross-cell cost.

    For B cells the v2 INDEX ships ``cost_breakdown.total_usd`` which is the
    sum of ``tokens_consumed`` cost + ``worker_cost_recorded`` cost. For A
    cells there is no worker_cost_recorded event (flat CLI emits a single
    ``tokens_consumed`` at run end), so ``total_cost_usd`` already covers
    everything.
    """
    cb = row.get("cost_breakdown") or {}
    if cb.get("total_usd") is not None:
        return float(cb["total_usd"])
    return float(row.get("total_cost_usd") or 0.0)


def _has_workspace_patch(run_dir: Path) -> bool:
    """Section 3.4.3 definition: ``workspace/model_patch.diff`` or equivalent
    ``fix.patch`` / ``cve-*.patch`` under ``workspace/``.
    """
    ws = run_dir / "workspace"
    if not ws.exists():
        return False
    if (ws / "model_patch.diff").exists():
        return True
    if (ws / "fix.patch").exists():
        return True
    for p in ws.glob("cve-*.patch"):
        if p.is_file():
            return True
    return False


def _corrected_e2e(run_dir: Path, cell: str, raw_e2e: bool) -> bool:
    """Apply A-cell sensitivity correction when ``corrected_mechanical.json`` exists.

    Matches the definition in report-0420.md §3.4.2:
    ``effective_pass_end_to_end = corrected_mechanical_end_to_end`` if the
    file exists (A cells only), else raw ``mech_end_to_end``. B cells fall
    through to ``raw_e2e`` (retrofit is already baked into the INDEX for the
    Pillar A / v1 release; v2 has not been retrofitted but also has no
    passes).
    """
    if cell.startswith("B"):
        return raw_e2e
    cm = run_dir / "corrected_mechanical.json"
    if not cm.exists():
        return raw_e2e
    try:
        data = json.loads(cm.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return raw_e2e
    val = data.get("end_to_end_pass")
    return bool(val) if val is not None else raw_e2e


def _row_for_run(row: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    cell = row["cell"]
    events_path = run_dir / "events.jsonl"
    rm = compute_run_metrics(run_dir, cell)
    tokens = _sum_tokens(events_path)
    mp = row.get("mechanical_pass") or {}
    mech_builder = bool(mp.get("builder"))
    mech_exploiter = bool(mp.get("exploiter"))
    mech_fixer = bool(mp.get("fixer"))
    mech_e2e = bool(mp.get("end_to_end"))
    has_ws_patch = _has_workspace_patch(run_dir)
    termination = row.get("termination_reason") or ""
    return {
        "cell": cell,
        "cve_id": row["cve_id"],
        "total_cost_usd": float(row.get("total_cost_usd") or 0.0),
        "true_cost_usd": _true_cost(row),
        "wallclock_seconds": float(row.get("wallclock_seconds") or 0.0),
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "cache_read_tokens": tokens["cache_read_tokens"],
        "cache_creation_tokens": tokens["cache_creation_tokens"],
        "event_count": int(row.get("event_count") or 0),
        "tool_calls_total": rm.tool_calls_total,
        "security_tool_adoption": rm.security_tool_adoption,
        "security_tool_invocations": rm.security_tool_invocations,
        "redundancy_intra": rm.redundancy_intra,
        "redundancy_sibling": rm.redundancy_sibling,
        "redundancy_hierarchy": rm.redundancy_hierarchy,
        "redundancy_rate_total": rm.redundancy_rate_total or 0.0,
        "cnr_mean": rm.cnr_mean if rm.cnr_mean is not None else float("nan"),
        "mech_builder": mech_builder,
        "mech_exploiter": mech_exploiter,
        "mech_fixer": mech_fixer,
        "mech_end_to_end": mech_e2e,
        "effective_pass_end_to_end": _corrected_e2e(run_dir, cell, mech_e2e),
        "workspace_has_patch": has_ws_patch,
        "no_patch_produced": (cell.startswith("B") and not has_ws_patch),
        "audit_violations": int(row.get("audit_violations") or 0),
        "termination_completed": termination == "completed",
    }


def build_runs_df() -> pd.DataFrame:
    v1 = _load_index(V1_INDEX)
    v2 = _load_index(V2_INDEX)
    rows: list[dict[str, Any]] = []

    for r in v1:
        if r["cell"] not in A_CELLS or r["replicate"] != 0:
            continue
        if r["cve_id"] not in PRIMARY_CVES:
            continue
        run_dir = Path(r["path"])
        if not run_dir.is_absolute():
            run_dir = REPO_ROOT / run_dir
        rows.append(_row_for_run(r, run_dir))

    for r in v2:
        if r["cell"] not in B_CELLS or r["replicate"] != 0:
            continue
        if r["cve_id"] not in PRIMARY_CVES:
            continue
        run_dir = Path(r["path"])
        if not run_dir.is_absolute():
            run_dir = REPO_ROOT / run_dir
        rows.append(_row_for_run(r, run_dir))

    df = pd.DataFrame(rows)
    # v1/v2 INDEX.jsonl are append-only; collapse duplicate (cell, cve_id) by
    # keeping the last occurrence, matching v2_analysis.py's convention.
    before = len(df)
    df = df.drop_duplicates(subset=["cell", "cve_id"], keep="last").reset_index(drop=True)
    if len(df) != before:
        sys.stderr.write(f"note: dropped {before - len(df)} duplicate INDEX row(s); kept 'last'\n")
    counts = df["cell"].value_counts().to_dict()
    for cell in (*A_CELLS, *B_CELLS):
        n = counts.get(cell, 0)
        if n != 10:
            raise AssertionError(
                f"expected N=10 for cell {cell} after filter+dedupe, got {n}"
            )
    return df


# Row definitions: (display label, column in df, aggregator, formatter).
# Aggregator is either "mean" (numeric / boolean-as-rate), or a callable.
def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_int(v: float) -> str:
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.1f}"


def _fmt_float(v: float, n: int = 2) -> str:
    return f"{v:.{n}f}"


def _fmt_rate(v: float) -> str:
    return f"{v:.2f}"


def _fmt_tokens(v: float) -> str:
    return f"{v:,.0f}"


METRIC_ROWS: list[tuple[str, str, str, Any]] = [
    # --- 3.1 Cost and resource footprint
    ("total_cost_usd [mean, $]", "total_cost_usd", "mean", _fmt_usd),
    ("true_cost_usd [mean, $]", "true_cost_usd", "mean", _fmt_usd),
    ("wallclock_seconds [mean]", "wallclock_seconds", "mean", _fmt_int),
    ("input_tokens [mean]", "input_tokens", "mean", _fmt_tokens),
    ("output_tokens [mean]", "output_tokens", "mean", _fmt_tokens),
    ("cache_read_tokens [mean]", "cache_read_tokens", "mean", _fmt_tokens),
    ("cache_creation_tokens [mean]", "cache_creation_tokens", "mean", _fmt_tokens),
    ("event_count [mean]", "event_count", "mean", _fmt_int),
    # --- 3.2 Tool-call activity
    ("tool_calls_total [mean]", "tool_calls_total", "mean", _fmt_int),
    ("security_tool_adoption [rate]", "security_tool_adoption", "mean", _fmt_rate),
    ("security_tool_invocations [mean]", "security_tool_invocations", "mean", _fmt_int),
    # --- 3.3 Redundancy
    ("redundancy_intra [mean]", "redundancy_intra", "mean", _fmt_int),
    ("redundancy_sibling [mean]", "redundancy_sibling", "mean", _fmt_int),
    ("redundancy_hierarchy [mean]", "redundancy_hierarchy", "mean", _fmt_int),
    ("redundancy_rate_total [mean]", "redundancy_rate_total", "mean", _fmt_float),
    # --- 3.4.1 Prompt efficiency
    ("cnr_mean [mean across runs]", "cnr_mean", "mean", _fmt_float),
    # --- 3.4.2 Mechanical evaluator
    ("mech_builder [pass rate]", "mech_builder", "mean", _fmt_rate),
    ("mech_exploiter [pass rate]", "mech_exploiter", "mean", _fmt_rate),
    ("mech_fixer [pass rate]", "mech_fixer", "mean", _fmt_rate),
    ("mech_end_to_end [pass rate]", "mech_end_to_end", "mean", _fmt_rate),
    ("effective_pass_end_to_end [pass rate]", "effective_pass_end_to_end", "mean", _fmt_rate),
    # --- 3.4.3 Deliverable production
    ("workspace_has_patch [rate]", "workspace_has_patch", "mean", _fmt_rate),
    ("no_patch_produced [rate, B cells only]", "no_patch_produced", "mean", _fmt_rate),
    # --- 3.4.4 Process integrity
    ("audit_violations [mean]", "audit_violations", "mean", _fmt_int),
    ("termination=completed [rate]", "termination_completed", "mean", _fmt_rate),
]


COLUMNS: list[tuple[str, list[str]]] = [
    ("A1", ["A1"]),
    ("A2", ["A2"]),
    ("B1", ["B1"]),
    ("B2", ["B2"]),
    ("A avg", ["A1", "A2"]),
    ("B avg", ["B1", "B2"]),
]


def _aggregate(df: pd.DataFrame, col: str, cells: list[str]) -> float:
    series = df.loc[df["cell"].isin(cells), col]
    if series.dtype == bool:
        series = series.astype(float)
    # Treat NaN in cnr_mean (e.g. no prompt_sent in flat CLI runs) as drop
    series = series.dropna()
    if series.empty:
        return float("nan")
    return float(series.mean())


def render_markdown(df: pd.DataFrame) -> str:
    header = "| Metric | " + " | ".join(label for label, _ in COLUMNS) + " |"
    sep = "|" + "---|" * (len(COLUMNS) + 1)
    lines = [header, sep]
    for label, col, _agg, fmt in METRIC_ROWS:
        cells: list[str] = []
        for _, cell_group in COLUMNS:
            v = _aggregate(df, col, cell_group)
            if v != v:  # NaN check
                cells.append("—")
            else:
                cells.append(fmt(v))
        lines.append("| " + label + " | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    df = build_runs_df()
    counts = df["cell"].value_counts().to_dict()
    sys.stderr.write(f"Loaded N={len(df)} runs  {counts}\n")
    print(render_markdown(df))


if __name__ == "__main__":
    main()
