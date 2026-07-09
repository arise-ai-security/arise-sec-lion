#!/usr/bin/env python
"""Run the subtree-modularization analysis over a study and write the report.

DB-grounded (Postgres `events`). One run per CVE (de-duplicated); success-only
by default. Outputs per-run JSON/CSV, an aggregate JSON, and a Markdown report.

    uv run python experiments/b1-batch-autogen/scripts/analyze_modularity.py \
        --exit-status success --n-perm 1000
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path

# Make the repo importable when this file is run directly (its dir name has a
# hyphen, so `python -m` is unavailable). Walk up to the pyproject.toml root.
_REPO_ROOT = Path(__file__).resolve()
while _REPO_ROOT != _REPO_ROOT.parent and not (_REPO_ROOT / "pyproject.toml").exists():
    _REPO_ROOT = _REPO_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from experiments.shared.scripts.analysis.modularity import report as report_mod  # noqa: E402
from experiments.shared.scripts.analysis.modularity import sources as src  # noqa: E402
from experiments.shared.scripts.analysis.modularity.claims import compute_run_metrics  # noqa: E402
from experiments.shared.scripts.analysis.modularity.discover import with_discovered_modules  # noqa: E402
from experiments.shared.scripts.analysis.modularity.figures import build_figures  # noqa: E402
from experiments.shared.scripts.analysis.modularity.normalize import build_run_model  # noqa: E402

_DIFF_TGT = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_DIFF_GIT = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)

_CSV_FIELDS = [
    "run_id", "task", "exit_status", "n_nodes", "n_modules",
    "modularity_labeled", "modularity_louvain", "recovery_nmi", "recovery_ari",
    "inter_module_fraction", "inter_module_message_count", "inter_module_dataflow_count",
    "recon_read_mean_jaccard", "global_writes_total",
    "ww_src", "ww_testcase", "ww_work", "pc_src", "pc_testcase",
    "perm_df_p", "perm_df_z", "perm_jaccard_p", "perm_ww_p",
]


def _patch_basenames(text: str) -> set[str]:
    files = set(_DIFF_TGT.findall(text)) | set(_DIFF_GIT.findall(text))
    return {os.path.basename(f.strip()) for f in files if f.strip() and f.strip() != "/dev/null"}


def _flat_row(metric: dict) -> dict:
    c, t, f, g, p = (metric["coupling"], metric["task_overlap"], metric["file_overlap"],
                     metric["global_context"], metric["permutation"])
    ww = f["write_write_overlap_by_zone"]
    pc = f["producer_consumer_edges_by_zone"]
    return {
        "run_id": metric["run_id"], "task": metric["task"], "exit_status": metric["exit_status"],
        "n_nodes": c["n_nodes"], "n_modules": c["n_modules"],
        "modularity_labeled": round(c["modularity_labeled"], 4),
        "modularity_louvain": round(c["modularity_louvain"], 4),
        "recovery_nmi": round(c["recovery_nmi"], 4), "recovery_ari": round(c["recovery_ari"], 4),
        "inter_module_fraction": round(c["inter_module_fraction"], 4),
        "inter_module_message_count": c["inter_module_message_count"],
        "inter_module_dataflow_count": c["inter_module_dataflow_count"],
        "recon_read_mean_jaccard": round(t["recon_read_mean_jaccard"], 4),
        "global_writes_total": g["global_writes_total"],
        "ww_src": ww.get("src", 0), "ww_testcase": ww.get("testcase", 0), "ww_work": ww.get("work", 0),
        "pc_src": pc.get("src", 0), "pc_testcase": pc.get("testcase", 0),
        "perm_df_p": _round(p["dataflow_inter_fraction"]["p_value_low"]),
        "perm_df_z": _round(p["dataflow_inter_fraction"]["z"]),
        "perm_jaccard_p": _round(p["recon_read_jaccard"]["p_value_low"]),
        "perm_ww_p": _round(p["write_write_overlap"]["p_value_low"]),
    }


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


async def run(args: argparse.Namespace) -> None:
    runs = src.enumerate_runs(args.runs_root, study_id=args.study)
    dedup = src.dedupe_by_cve(runs)
    statuses = None if args.exit_status == "all" else set(args.exit_status.split(","))
    selected = [r for r in dedup.chosen
                if r.has_events and (statuses is None or r.exit_status in statuses)]
    if args.limit:
        selected = selected[: args.limit]
    print(f"{args.study}: {len(runs)} runs -> {len(dedup.chosen)} unique CVEs "
          f"-> {len(selected)} selected (exit_status={args.exit_status})")

    per_run: list[dict] = []
    models: list = []
    patch_recalls: list[float] = []
    conn = await src.open_db()
    try:
        for run_info in selected:
            events = await src.load_events_db(conn, run_info.run_id)
            src.assert_root_matches(run_info.run_id, events)
            shared = await src.load_shared_store_db(conn, run_info.run_id)
            model = build_run_model(run_info.run_id, run_info.task, run_info.exit_status, events, shared)
            if args.strategy == "discovered":
                model = with_discovered_modules(model)
            per_run.append(compute_run_metrics(model, n_perm=args.n_perm))
            models.append(model)
            patch = run_info.run_dir / "testcase" / "model_patch.diff"
            if patch.is_file():
                gt = _patch_basenames(patch.read_text(errors="ignore"))
                if gt:
                    wb = {os.path.basename(t.path) for t in model.touches
                          if t.op in ("write", "edit") and t.confidence == "high" and t.path}
                    patch_recalls.append(len(gt & wb) / len(gt))
    finally:
        await conn.close()

    agg = report_mod.aggregate(per_run)
    provenance = {
        "total_runs": len(runs), "deduped": len(dedup.chosen), "mismatches": 0,
        "multi_success": len(dedup.multi_success),
        "patch_recall": f"{sum(patch_recalls) / len(patch_recalls):.3f}" if patch_recalls else "n/a",
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "per_run.json").write_text(json.dumps(per_run, indent=2, default=str))
    (out / "aggregate.json").write_text(json.dumps(agg, indent=2, default=str))
    with (out / "per_run.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_flat_row(m) for m in per_run)

    figures, profiles, module_flow = build_figures(per_run, agg, models, out / "figures")
    extra = {"figures": figures, "profiles": profiles, "module_flow": module_flow}
    (out / "FIGURES.md").write_text(
        "# Figures\n\n" + "\n\n".join(
            f"## {name}\n\n![{name}](figures/{name})\n\n{cap}" for name, cap in figures))
    (out / "report.md").write_text(
        report_mod.render_markdown(agg, study=args.study, provenance=provenance, extra=extra))

    print(f"wrote {out}/report.md, FIGURES.md, figures/ ({len(figures)} SVGs)")
    c = agg["coupling"]
    print(f"  recovery NMI median={c['recovery_nmi'].get('median')!r} "
          f"ARI median={c['recovery_ari'].get('median')!r} "
          f"Q_labeled median={c['modularity_labeled'].get('median')!r}")
    print(f"  inter-module messages total={c['inter_module_message_count_total']} (structural 0)")
    print(f"  dataflow-inter-fraction null: {agg['permutation']['dataflow_inter_fraction']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Subtree-modularization analysis over a study.")
    parser.add_argument("--study", default="b1-batch-autogen")
    parser.add_argument("--runs-root", default="runs")
    parser.add_argument("--exit-status", default="success",
                        help="'success' (default), 'all', or comma list e.g. 'success,timeout'")
    parser.add_argument("--n-perm", type=int, default=1000)
    parser.add_argument("--strategy", choices=["labeled", "discovered"], default="labeled",
                        help="'labeled' (B1 bracket modules) or 'discovered' (A1/A2 communities)")
    parser.add_argument("--limit", type=int, default=0, help="cap runs (debug)")
    parser.add_argument("--out", default="experiments/b1-batch-autogen/reports/modularity")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
