"""Category-A anti-cheat violation examples for the tree-vs-flat report.

Walks every A1 / A2 ``audit.json`` across the 10 primary CVEs (replicate 0)
and extracts concrete, verbatim violations from the event store. For each
violation ``type`` (per ``experiments/audit_cheating.py``) the script:

* Tallies per-cell totals and cross-checks them against
  ``dataset/INDEX.jsonl[audit_violations]``.
* Pulls up to 3 representative verbatim examples -- full ``Bash``/``WebFetch``
  ``tool_input`` as stored in ``events.jsonl``, with ``run_id`` and ``CVE``.
  Examples are selected to prefer distinct CVEs so breadth is visible.
* Emits a markdown section with a type x cell count table followed by
  per-type subsections that quote the examples verbatim.

Scope: cells A1 and A2 only. Category B is explicitly NOT included: the
``infrastructure/adapters/worker/claude_sdk_adapter.py`` path reaching the
Claude SDK hook loses ``tool_name`` / ``tool_input`` (records them as
``"Tool"`` / ``{}``) so the auditor sees zero violations for B runs. A zero
there is an instrumentation artefact, not evidence of clean behaviour.

Outputs:
* ``docs/pillar_b/tables/a_cheating_by_type.md``    -- type x cell count table
* ``docs/pillar_b/tables/a_cheating_by_type.csv``   -- raw numbers
* ``docs/pillar_b/examples/a_cheating_examples.md`` -- markdown with verbatim
  tool_input blocks per violation type

Usage::

    uv run python docs/pillar_b/scripts/a_cheating_examples.py
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "dataset" / "runs"
INDEX_PATH = REPO_ROOT / "dataset" / "INDEX.jsonl"
TABLES_DIR = REPO_ROOT / "docs" / "pillar_b" / "tables"
EXAMPLES_DIR = REPO_ROOT / "docs" / "pillar_b" / "examples"

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
EXAMPLES_PER_TYPE = 3

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("a_cheating_examples")


def _load_events_by_id(events_path: Path) -> dict[str, dict[str, Any]]:
    """Index events.jsonl by event_id. Tolerates malformed lines."""
    out: dict[str, dict[str, Any]] = {}
    if not events_path.exists():
        return out
    for line in events_path.open(encoding="utf-8"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("malformed line in %s", events_path)
            continue
        eid = ev.get("event_id")
        if eid:
            out[eid] = ev
    return out


def _verbatim_from_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Return the full tool_input payload for a violation event.

    The auditor stores only the regex-matched substring for Bash (``matched``)
    or the minimal dict for WebFetch (``tool_input``). To satisfy the report
    requirement of verbatim, non-truncated examples we pull directly from the
    events.jsonl record so the complete Bash command or WebFetch URL+prompt is
    preserved.
    """
    payload = ev.get("payload") or {}
    tool_input = payload.get("tool_input") or {}
    return {
        "tool_name": payload.get("tool_name"),
        "tool_input": tool_input,
        "sequence_number": ev.get("sequence_number"),
        "occurred_at": ev.get("occurred_at"),
    }


def collect() -> dict[str, Any]:
    """Walk every A-cell primary-CVE run and collect audit violations.

    Returns a dict with:
    * ``per_cell_counts``: dict[cell, Counter[type]] -- violation tallies.
    * ``per_cell_total``: dict[cell, int] -- sum of violation_count across runs.
    * ``examples``: dict[type, list[example]] -- verbatim records, ordered.
    * ``index_totals``: dict[cell, int] -- sum of INDEX.jsonl audit_violations
      for the same runs (for cross-check).
    * ``missing``: list[(cve, cell)] -- runs lacking audit.json.
    """
    per_cell_counts: dict[str, Counter[str]] = {c: Counter() for c in A_CELLS}
    per_cell_total: dict[str, int] = {c: 0 for c in A_CELLS}
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing: list[tuple[str, str]] = []

    # First pass: INDEX totals for cross-check.
    index_totals: dict[str, int] = {c: 0 for c in A_CELLS}
    index_per_run: dict[str, int] = {}
    if INDEX_PATH.exists():
        for line in INDEX_PATH.open(encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            if row["cell"] not in A_CELLS:
                continue
            if row["cve_id"] not in PRIMARY_CVES:
                continue
            if str(row["replicate"]) != REPLICATE:
                continue
            index_totals[row["cell"]] += int(row["audit_violations"])
            index_per_run[row["run_id"]] = int(row["audit_violations"])

    # Second pass: audit.json + events.jsonl per run.
    for cve in PRIMARY_CVES:
        for cell in A_CELLS:
            run_dir = RUNS_ROOT / cve / cell / REPLICATE
            audit_path = run_dir / "audit.json"
            events_path = run_dir / "events.jsonl"
            if not audit_path.exists():
                missing.append((cve, cell))
                continue
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            viols = audit.get("violations") or []
            per_cell_total[cell] += int(audit.get("violation_count", len(viols)))
            events_idx = _load_events_by_id(events_path)
            run_id = f"{cve}-{cell}-{REPLICATE}"

            for v in viols:
                vtype = v["type"]
                per_cell_counts[cell][vtype] += 1
                ev = events_idx.get(v.get("event_id", ""))
                verbatim = _verbatim_from_event(ev) if ev else {
                    "tool_name": None,
                    "tool_input": None,
                    "sequence_number": None,
                    "occurred_at": None,
                }
                examples[vtype].append({
                    "cve": cve,
                    "cell": cell,
                    "run_id": run_id,
                    "event_id": v.get("event_id"),
                    "audit_matched": v.get("matched") or v.get("url"),
                    "verbatim": verbatim,
                })

    # Cross-check total against INDEX.
    for cell in A_CELLS:
        audit_sum = per_cell_total[cell]
        idx_sum = index_totals[cell]
        if audit_sum != idx_sum:
            logger.warning(
                "cell %s: audit.json sum=%d differs from INDEX sum=%d",
                cell,
                audit_sum,
                idx_sum,
            )
        else:
            logger.info(
                "cell %s: audit.json sum=%d matches INDEX sum=%d",
                cell,
                audit_sum,
                idx_sum,
            )

    return {
        "per_cell_counts": per_cell_counts,
        "per_cell_total": per_cell_total,
        "examples": examples,
        "index_totals": index_totals,
        "index_per_run": index_per_run,
        "missing": missing,
    }


def _choose_examples(
    records: list[dict[str, Any]],
    k: int = EXAMPLES_PER_TYPE,
) -> list[dict[str, Any]]:
    """Pick up to k examples, preferring breadth across CVEs / cells.

    Greedy: first picks from CVEs not yet represented; then backfills from
    any remaining records. Preserves original order for stable output.
    """
    picked: list[dict[str, Any]] = []
    seen_cve: set[str] = set()
    for r in records:
        if len(picked) >= k:
            break
        if r["cve"] in seen_cve:
            continue
        picked.append(r)
        seen_cve.add(r["cve"])
    if len(picked) < k:
        remaining = [r for r in records if r not in picked]
        picked.extend(remaining[: k - len(picked)])
    return picked


def _fmt_tool_input(tool_name: str | None, tool_input: Any) -> str:
    """Render tool_input as a fenced block preserving exact text.

    * ``Bash`` -> show ``command`` unmodified in a ```bash block.
    * ``WebFetch`` -> show the full JSON dict in a ```json block so url+prompt
      are both visible and non-truncated.
    * Anything else -> JSON dump.
    """
    if tool_input is None:
        return "```\n(event not found in events.jsonl)\n```"
    if tool_name == "Bash" and isinstance(tool_input, dict) and "command" in tool_input:
        cmd = tool_input["command"]
        return f"```bash\n{cmd}\n```"
    if tool_name == "WebFetch" and isinstance(tool_input, dict):
        return "```json\n" + json.dumps(tool_input, indent=2, ensure_ascii=False) + "\n```"
    return "```json\n" + json.dumps(tool_input, indent=2, ensure_ascii=False) + "\n```"


def _write_count_table(path_md: Path, path_csv: Path, data: dict[str, Any]) -> None:
    counts: dict[str, Counter[str]] = data["per_cell_counts"]
    totals: dict[str, int] = data["per_cell_total"]
    idx_totals: dict[str, int] = data["index_totals"]
    types = sorted({t for c in counts.values() for t in c})

    lines: list[str] = []
    lines.append(f"# A-cell audit violations by type ({len(PRIMARY_CVES)} CVEs, replicate {REPLICATE})\n")
    lines.append("Cross-checked against `dataset/INDEX.jsonl[audit_violations]`.\n")
    lines.append("| type | A1 | A2 |")
    lines.append("|---|---|---|")
    for t in types:
        lines.append(f"| `{t}` | {counts['A1'].get(t, 0)} | {counts['A2'].get(t, 0)} |")
    lines.append(f"| **TOTAL (audit.json)** | **{totals['A1']}** | **{totals['A2']}** |")
    lines.append(f"| **TOTAL (INDEX.jsonl)** | **{idx_totals['A1']}** | **{idx_totals['A2']}** |")
    lines.append("")
    path_md.parent.mkdir(parents=True, exist_ok=True)
    path_md.write_text("\n".join(lines), encoding="utf-8")

    path_csv.parent.mkdir(parents=True, exist_ok=True)
    with path_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["type", "A1", "A2"])
        for t in types:
            w.writerow([t, counts["A1"].get(t, 0), counts["A2"].get(t, 0)])
        w.writerow(["TOTAL_audit", totals["A1"], totals["A2"]])
        w.writerow(["TOTAL_index", idx_totals["A1"], idx_totals["A2"]])


def _write_examples_markdown(path: Path, data: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# Verbatim A-cell cheating-attempt examples\n")
    lines.append(
        "Each block is the **exact** `tool_input` payload as recorded in the "
        "run's `events.jsonl`. No truncation, no redaction. `event_id` and "
        "`run_id` identify the source record.\n"
    )
    lines.append(
        "Caveat: **B-cell anti-cheat counts are not measurable**. The Claude "
        "SDK hook path in `infrastructure/adapters/worker/claude_sdk_adapter.py` "
        "records `tool_name=\"Tool\"` and `tool_input={}`, so neither Bash "
        "commands nor WebFetch URLs survive into the event store for B1/B2. "
        "The auditor cannot match an empty string, which is why the audit "
        "violation counts are zero for B cells -- that zero is an "
        "instrumentation artefact, not evidence of clean behaviour.\n"
    )

    examples: dict[str, list[dict[str, Any]]] = data["examples"]
    for vtype in sorted(examples):
        records = examples[vtype]
        picked = _choose_examples(records)
        lines.append(f"## `{vtype}` ({len(records)} total across A1+A2)\n")
        for i, r in enumerate(picked, 1):
            verbatim = r["verbatim"]
            lines.append(
                f"**Example {i}** -- CVE `{r['cve']}`, cell `{r['cell']}`, "
                f"`run_id={r['run_id']}`, `event_id={r['event_id']}`, "
                f"tool=`{verbatim.get('tool_name')}`, "
                f"seq={verbatim.get('sequence_number')}.\n"
            )
            lines.append(_fmt_tool_input(verbatim.get("tool_name"), verbatim.get("tool_input")))
            lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    data = collect()
    if data["missing"]:
        logger.warning("missing audit.json for: %s", data["missing"])

    for cell in A_CELLS:
        logger.info(
            "cell %s: violations=%d, by_type=%s",
            cell,
            data["per_cell_total"][cell],
            dict(data["per_cell_counts"][cell]),
        )

    _write_count_table(
        TABLES_DIR / "a_cheating_by_type.md",
        TABLES_DIR / "a_cheating_by_type.csv",
        data,
    )
    _write_examples_markdown(
        EXAMPLES_DIR / "a_cheating_examples.md",
        data,
    )
    logger.info(
        "wrote %s and %s",
        TABLES_DIR / "a_cheating_by_type.md",
        EXAMPLES_DIR / "a_cheating_examples.md",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
