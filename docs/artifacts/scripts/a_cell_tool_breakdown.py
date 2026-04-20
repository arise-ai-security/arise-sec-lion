"""A-cell tool-call breakdown for the tree-vs-flat report.

Computes, for Category A (flat Claude Code CLI -- cells A1 and A2):
* Per-cell distribution of ``tool_use`` events by ``payload.tool_name`` --
  absolute count, per-cell per-run mean, and share of total ``tool_use``.
* Within ``Bash``, the top-15 command prefixes by first-three-token
  normalisation (matching ``docs/report-0420.md`` §3.3) per cell.

Scope: replicate 0 of the 10 primary CVEs locked by Pillar B. Category B is
explicitly excluded because ``infrastructure/adapters/worker/claude_sdk_adapter.py``
records ``tool_name="Tool"`` / ``tool_input={}`` for SDK tool calls, so B
events cannot be decomposed by tool. See
``docs/superpowers/plans/2026-04-19-pillar-b-rerun.md`` (lines 304-309).

The script writes:
* ``docs/pillar_b/tables/a_cell_tool_name_distribution.md``  -- tool-name table
* ``docs/pillar_b/tables/a_cell_bash_prefixes.md``           -- Bash prefix table
* ``docs/pillar_b/tables/a_cell_tool_name_distribution.csv`` -- raw numbers
* ``docs/pillar_b/tables/a_cell_bash_prefixes.csv``          -- raw numbers

Usage::

    uv run python docs/pillar_b/scripts/a_cell_tool_breakdown.py
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "dataset" / "runs"
TABLES_DIR = REPO_ROOT / "docs" / "pillar_b" / "tables"

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
A_CELLS: tuple[str, ...] = ("A1", "A2")
REPLICATE = "0"
BASH_TOP_N = 15

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("a_cell_tool_breakdown")


def _iter_events(path: Path):
    if not path.exists():
        logger.warning("missing events.jsonl: %s", path)
        return
    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _bash_prefix(command: str) -> str:
    """Normalise a Bash command to its first-three-token prefix (§3.3).

    We intentionally do not split on ``|``, ``&&``, ``;``, or command
    substitution. The §3.3 spec is "first three tokens of ``command``" --
    splitting on the shell operators would be a second-layer normalisation
    that compute_metrics.py does not apply for its redundancy target. We
    therefore keep the prefix identical across this script and the existing
    redundancy computation so the two views are consistent.
    """
    toks = command.strip().split()
    if not toks:
        return "(empty)"
    return " ".join(toks[:3])


def collect_cell(cell: str) -> dict:
    """Aggregate tool_use counts for one A-cell across all primary CVEs.

    Returns a dict with:
    * ``per_run_tool_counts``: list[Counter[str]] -- one entry per CVE run.
    * ``per_run_bash_prefixes``: list[Counter[str]] -- one per CVE run.
    * ``missing``: list[str] -- CVEs whose events.jsonl was not found.
    * ``synthetic_dropped``: int -- events filtered per §3.2 (payload.data is dict).
    * ``unnamed_dropped``: int -- tool_use events with empty / "?" tool_name.
    """
    per_run_tool_counts: list[Counter[str]] = []
    per_run_bash_prefixes: list[Counter[str]] = []
    missing: list[str] = []
    synthetic_dropped = 0
    unnamed_dropped = 0

    for cve in PRIMARY_CVES:
        run_dir = RUNS_ROOT / cve / cell / REPLICATE
        events_path = run_dir / "events.jsonl"
        if not events_path.exists():
            missing.append(cve)
            per_run_tool_counts.append(Counter())
            per_run_bash_prefixes.append(Counter())
            continue

        tc: Counter[str] = Counter()
        bp: Counter[str] = Counter()
        for ev in _iter_events(events_path):
            if ev.get("event_type") != "tool_use":
                continue
            payload = ev.get("payload") or {}
            # §3.2: drop synthetic tree-domain wrappers.
            if isinstance(payload.get("data"), dict):
                synthetic_dropped += 1
                continue
            tn = payload.get("tool_name")
            if not tn or tn == "?":
                unnamed_dropped += 1
                continue
            tc[tn] += 1
            if tn == "Bash":
                cmd = (payload.get("tool_input") or {}).get("command", "")
                if isinstance(cmd, str):
                    bp[_bash_prefix(cmd)] += 1
        per_run_tool_counts.append(tc)
        per_run_bash_prefixes.append(bp)

    return {
        "cell": cell,
        "per_run_tool_counts": per_run_tool_counts,
        "per_run_bash_prefixes": per_run_bash_prefixes,
        "missing": missing,
        "synthetic_dropped": synthetic_dropped,
        "unnamed_dropped": unnamed_dropped,
    }


def _tool_name_rows(by_cell: dict[str, dict]) -> list[dict]:
    """Union tool-name set, then emit rows sorted by total share (desc)."""
    all_tools: set[str] = set()
    for data in by_cell.values():
        for c in data["per_run_tool_counts"]:
            all_tools.update(c)

    rows: list[dict] = []
    # Totals per cell (to compute percentages).
    totals = {cell: sum(sum(c.values()) for c in data["per_run_tool_counts"])
              for cell, data in by_cell.items()}
    for tool in sorted(all_tools):
        row: dict = {"tool_name": tool}
        for cell, data in by_cell.items():
            counts = [c.get(tool, 0) for c in data["per_run_tool_counts"]]
            total = sum(counts)
            row[f"{cell}_total"] = total
            row[f"{cell}_mean_per_run"] = mean(counts) if counts else 0.0
            row[f"{cell}_pct"] = (100.0 * total / totals[cell]) if totals[cell] else 0.0
        rows.append(row)

    # Sort by A1 share descending (then A2 share as tiebreaker).
    rows.sort(key=lambda r: (-r.get("A1_pct", 0.0), -r.get("A2_pct", 0.0)))
    return rows


def _bash_prefix_rows(by_cell: dict[str, dict], top_n: int = BASH_TOP_N) -> dict[str, list[dict]]:
    """Per-cell top-N Bash prefix rows with counts and share of Bash total."""
    out: dict[str, list[dict]] = {}
    for cell, data in by_cell.items():
        agg: Counter[str] = Counter()
        for c in data["per_run_bash_prefixes"]:
            agg.update(c)
        bash_total = sum(agg.values())
        rows = []
        for prefix, count in agg.most_common(top_n):
            rows.append({
                "prefix": prefix,
                "count": count,
                "pct_of_bash": (100.0 * count / bash_total) if bash_total else 0.0,
            })
        out[cell] = rows
    return out


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|")


def _write_tool_name_markdown(path: Path, rows: list[dict], by_cell: dict[str, dict]) -> None:
    cells = sorted(by_cell)
    totals = {cell: sum(sum(c.values()) for c in by_cell[cell]["per_run_tool_counts"])
              for cell in cells}
    n_runs = {cell: len(by_cell[cell]["per_run_tool_counts"]) for cell in cells}

    lines: list[str] = []
    lines.append(f"# A-cell tool-name distribution ({', '.join(PRIMARY_CVES)})\n")
    lines.append(f"Replicate {REPLICATE}. {len(PRIMARY_CVES)} CVEs per cell. "
                 "Synthetic tree-domain wrappers (payload.data is dict) are filtered per §3.2.\n")

    header = ["tool_name"]
    for cell in cells:
        header.extend([f"{cell} total", f"{cell} mean/run", f"{cell} %"])
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    for r in rows:
        cols = [_md_escape(r["tool_name"])]
        for cell in cells:
            cols.append(str(r[f"{cell}_total"]))
            cols.append(f"{r[f'{cell}_mean_per_run']:.2f}")
            cols.append(f"{r[f'{cell}_pct']:.1f}")
        lines.append("| " + " | ".join(cols) + " |")

    # Totals row
    total_cols = ["**TOTAL**"]
    for cell in cells:
        total_cols.append(f"**{totals[cell]}**")
        total_cols.append(f"**{totals[cell] / n_runs[cell]:.2f}**" if n_runs[cell] else "-")
        total_cols.append("**100.0**")
    lines.append("| " + " | ".join(total_cols) + " |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_bash_prefix_markdown(path: Path, per_cell_rows: dict[str, list[dict]]) -> None:
    lines: list[str] = ["# A-cell Bash command-prefix breakdown (top {n}/cell)\n".format(n=BASH_TOP_N)]
    lines.append("Prefix = first three whitespace-split tokens of the Bash `command` "
                 "(matches `docs/report-0420.md` §3.3). Ranked within each cell by raw count.\n")
    for cell in sorted(per_cell_rows):
        rows = per_cell_rows[cell]
        total = sum(r["count"] for r in rows)
        lines.append(f"## {cell} (top-{BASH_TOP_N}; n shown = {total} Bash calls)\n")
        lines.append("| rank | prefix | count | % of Bash |")
        lines.append("|---|---|---|---|")
        for idx, r in enumerate(rows, 1):
            lines.append(f"| {idx} | `{_md_escape(r['prefix'])}` | {r['count']} | {r['pct_of_bash']:.1f} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_tool_name_csv(path: Path, rows: list[dict], cells: list[str]) -> None:
    fieldnames = ["tool_name"]
    for cell in cells:
        fieldnames.extend([f"{cell}_total", f"{cell}_mean_per_run", f"{cell}_pct"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_bash_prefix_csv(path: Path, per_cell_rows: dict[str, list[dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["cell", "rank", "prefix", "count", "pct_of_bash"])
        for cell in sorted(per_cell_rows):
            for idx, r in enumerate(per_cell_rows[cell], 1):
                writer.writerow([cell, idx, r["prefix"], r["count"], f"{r['pct_of_bash']:.3f}"])


def main() -> int:
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    by_cell: dict[str, dict] = {cell: collect_cell(cell) for cell in A_CELLS}

    for cell, data in by_cell.items():
        totals = sum(sum(c.values()) for c in data["per_run_tool_counts"])
        logger.info("%s: %d runs loaded, %d tool_use after filtering, "
                    "synthetic_dropped=%d, unnamed_dropped=%d, missing=%s",
                    cell, len(data["per_run_tool_counts"]) - len(data["missing"]),
                    totals, data["synthetic_dropped"], data["unnamed_dropped"],
                    data["missing"] or "none")

    rows = _tool_name_rows(by_cell)
    per_cell_prefix = _bash_prefix_rows(by_cell)

    _write_tool_name_markdown(TABLES_DIR / "a_cell_tool_name_distribution.md", rows, by_cell)
    _write_bash_prefix_markdown(TABLES_DIR / "a_cell_bash_prefixes.md", per_cell_prefix)
    _write_tool_name_csv(TABLES_DIR / "a_cell_tool_name_distribution.csv", rows, list(A_CELLS))
    _write_bash_prefix_csv(TABLES_DIR / "a_cell_bash_prefixes.csv", per_cell_prefix)

    # Percentage-sum sanity check (numerical drift <0.05 pp is fine).
    for cell in A_CELLS:
        pct_sum = sum(r[f"{cell}_pct"] for r in rows)
        if abs(pct_sum - 100.0) > 0.05 and pct_sum != 0.0:
            logger.warning("cell %s: tool-name percentages sum to %.4f, not 100.0", cell, pct_sum)
        else:
            logger.info("cell %s: percentages sum to %.4f (OK)", cell, pct_sum)
        bash_pct_sum = sum(r["pct_of_bash"] for r in per_cell_prefix[cell])
        logger.info("cell %s: top-%d Bash prefixes cover %.1f%% of Bash calls",
                    cell, BASH_TOP_N, bash_pct_sum)

    logger.info("wrote outputs under %s", TABLES_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
