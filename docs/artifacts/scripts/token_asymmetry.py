"""Explain the A-vs-B token / cost asymmetry in Section 4 of report-0420.md.

Question
--------
Section 4 shows a striking pattern across the 10 primary CVEs (replicate 0):

* A1 input=390,    output=65,621  -> total_cost ~$4.1
* A2 input=514,    output=92,598  -> total_cost ~$6.1
* B1 input=82,072, output=39,033  -> total_cost ~$5.6
* B2 input=193,376,output=28,581  -> total_cost ~$4.9

Input/output token mix diverges by orders of magnitude, yet cost converges
in the $4-$6 band. This script decomposes each cell's token totals into
four priced classes (input / output / cache_read / cache_create), splits
B cells by the event family that produced the tokens
(``tokens_consumed`` vs ``worker_cost_recorded``), and shows which class
dominates the dollar bill per cell.

Data sources
------------
* A1/A2: ``dataset/runs/<cve>/<cell>/0/events.jsonl`` -- one terminal
  ``tokens_consumed`` event per run with aggregate Sonnet usage.
* B1/B2: ``dataset-v2-20260420/runs/<cve>/<cell>/0/events.jsonl`` --
  many ``tokens_consumed`` events (BOSS / condense, models Opus 4.7 and
  gpt-4o-mini) plus many ``worker_cost_recorded`` events (WORKER, Sonnet
  4.6 via Claude Code wrapper). ``tokens_consumed`` and
  ``worker_cost_recorded`` use different payload keys for the same
  semantic counters; both are normalised to a shared schema below.

Pricing
-------
The user prompt fixes the rate table to **Anthropic Claude Sonnet 4.6
list rates** (USD per MTok):

* input:        $3.00
* output:       $15.00
* cache_read:   $0.30
* cache_create: $3.75

These are declared as named constants (``SONNET_*_USD_PER_MTOK``) and
used uniformly for every bucket so the comparison across cells is on a
single, common cost yardstick. This deliberately over-prices the
gpt-4o-mini condense traffic and slightly under-prices the Opus BOSS
traffic; see the ``Open questions`` section in the report fragment for
follow-ups. The per-model actuals live in each run's emitted
``cost_usd`` fields and are surfaced as the ``$ reported`` column for
reviewer cross-checking.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
V1_RUNS = ROOT / "dataset" / "runs"
V2_RUNS = ROOT / "dataset-v2-20260420" / "runs"
V2_INDEX = ROOT / "dataset-v2-20260420" / "INDEX.jsonl"

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

# Anthropic Claude Sonnet 4.6 list rates, USD per megatoken. Source:
# https://www.anthropic.com/pricing. One common yardstick for every bucket
# so the per-class dollar comparison is apples-to-apples across A and B.
SONNET_INPUT_USD_PER_MTOK: float = 3.00
SONNET_OUTPUT_USD_PER_MTOK: float = 15.00
SONNET_CACHE_READ_USD_PER_MTOK: float = 0.30
SONNET_CACHE_CREATE_USD_PER_MTOK: float = 3.75

MTOK: float = 1_000_000.0


# =============================================================================
# Event scanning
# =============================================================================


def _empty_counter() -> dict[str, float]:
    return defaultdict(float)


def _scan_a_run(events_path: Path) -> dict[str, float]:
    """Aggregate the terminal ``tokens_consumed`` event of a flat-cell run.

    Flat A runs emit exactly one summary ``tokens_consumed`` event whose
    payload totals every Claude Sonnet turn. Runs that terminated at the
    wallclock cap may emit zero such events (e.g. A1/gpac.cve-2022-1795);
    we return an empty counter so the run contributes zero while still
    counting toward N=10 -- matching section_4_results.py convention.
    """
    agg = _empty_counter()
    if not events_path.exists():
        return agg
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "tokens_consumed":
            continue
        payload = event.get("payload") or {}
        agg["input_tokens"] += payload.get("input_tokens") or 0
        agg["output_tokens"] += payload.get("output_tokens") or 0
        agg["cache_read_tokens"] += payload.get("cache_read_input_tokens") or 0
        agg["cache_create_tokens"] += payload.get("cache_creation_input_tokens") or 0
        agg["reported_cost_usd"] += payload.get("cost_usd") or 0
        agg["event_count"] += 1
    return agg


def _scan_b_run(events_path: Path) -> dict[str, dict[str, float]]:
    """Split a tree-cell run by event family.

    Returns two counters with a shared token-name schema:

    * ``tokens_consumed`` -- BOSS / MANAGER LLM calls (Opus 4.7 decompose,
      gpt-4o-mini condense / subtask_parse). Payload uses
      ``cache_read_input_tokens`` / ``cache_creation_input_tokens``.
    * ``worker_cost_recorded`` -- WORKER role, Claude Code wrapper against
      Sonnet 4.6. Payload uses ``prompt_tokens`` / ``completion_tokens``
      and ``cache_read_tokens`` / ``cache_write_tokens``.

    A side counter ``tokens_consumed_by_model`` retains the per-model
    breakdown (Opus vs mini) purely as diagnostic output. The primary
    dollar numbers price every bucket with Sonnet 4.6 list rates per the
    prompt's directive.
    """
    buckets: dict[str, dict[str, float]] = {
        "tokens_consumed": _empty_counter(),
        "worker_cost_recorded": _empty_counter(),
    }
    by_model: dict[str, dict[str, float]] = defaultdict(_empty_counter)
    if not events_path.exists():
        return {**buckets, "_tokens_consumed_by_model": dict(by_model)}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if event_type == "tokens_consumed":
            bucket = buckets["tokens_consumed"]
            model = payload.get("model") or "unknown"
            model_bucket = by_model[model]
            in_tok = payload.get("input_tokens") or 0
            out_tok = payload.get("output_tokens") or 0
            cr_tok = payload.get("cache_read_input_tokens") or 0
            cc_tok = payload.get("cache_creation_input_tokens") or 0
            cost = payload.get("cost_usd") or 0
            bucket["input_tokens"] += in_tok
            bucket["output_tokens"] += out_tok
            bucket["cache_read_tokens"] += cr_tok
            bucket["cache_create_tokens"] += cc_tok
            bucket["reported_cost_usd"] += cost
            bucket["event_count"] += 1
            model_bucket["input_tokens"] += in_tok
            model_bucket["output_tokens"] += out_tok
            model_bucket["cache_read_tokens"] += cr_tok
            model_bucket["cache_create_tokens"] += cc_tok
            model_bucket["reported_cost_usd"] += cost
            model_bucket["event_count"] += 1
        elif event_type == "worker_cost_recorded":
            bucket = buckets["worker_cost_recorded"]
            bucket["input_tokens"] += payload.get("prompt_tokens") or 0
            bucket["output_tokens"] += payload.get("completion_tokens") or 0
            bucket["cache_read_tokens"] += payload.get("cache_read_tokens") or 0
            bucket["cache_create_tokens"] += payload.get("cache_write_tokens") or 0
            bucket["reported_cost_usd"] += payload.get("cost_usd") or 0
            bucket["event_count"] += 1
    return {**buckets, "_tokens_consumed_by_model": dict(by_model)}


def _index_b_total(index_path: Path, cve: str, cell: str) -> float | None:
    """Last-write-wins ``cost_breakdown.total_usd`` from INDEX.jsonl."""
    if not index_path.exists():
        return None
    latest: float | None = None
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            row.get("cve_id") == cve
            and row.get("cell") == cell
            and row.get("replicate") == 0
        ):
            cb = row.get("cost_breakdown") or {}
            total = cb.get("total_usd")
            if total is not None:
                latest = float(total)
    return latest


# =============================================================================
# Aggregation
# =============================================================================


def build_per_cell() -> dict[str, dict[str, Any]]:
    """Aggregate per-cell totals across the 10 primary CVEs (N=10 always)."""
    cells: dict[str, dict[str, Any]] = {}

    for cell in ("A1", "A2"):
        totals = _empty_counter()
        missing: list[str] = []
        for cve in PRIMARY_CVES:
            events_path = V1_RUNS / cve / cell / "0" / "events.jsonl"
            run_agg = _scan_a_run(events_path)
            if run_agg.get("event_count", 0) == 0:
                missing.append(cve)
            for key, value in run_agg.items():
                totals[key] += value
        cells[cell] = {
            "n": len(PRIMARY_CVES),
            "buckets": {"flat_cli_tokens_consumed": totals},
            "missing_runs": missing,
        }

    for cell in ("B1", "B2"):
        agg_buckets: dict[str, dict[str, float]] = {
            "tokens_consumed": _empty_counter(),
            "worker_cost_recorded": _empty_counter(),
        }
        agg_by_model: dict[str, dict[str, float]] = defaultdict(_empty_counter)
        index_total_sum = 0.0
        for cve in PRIMARY_CVES:
            events_path = V2_RUNS / cve / cell / "0" / "events.jsonl"
            run_buckets = _scan_b_run(events_path)
            for bname, bvals in run_buckets.items():
                if bname == "_tokens_consumed_by_model":
                    for model, mvals in bvals.items():
                        for key, value in mvals.items():
                            agg_by_model[model][key] += value
                    continue
                for key, value in bvals.items():
                    agg_buckets[bname][key] += value
            idx_total = _index_b_total(V2_INDEX, cve, cell)
            if idx_total is not None:
                index_total_sum += idx_total
        cells[cell] = {
            "n": len(PRIMARY_CVES),
            "buckets": agg_buckets,
            "tokens_consumed_by_model": dict(agg_by_model),
            "index_total_sum": index_total_sum,
            "missing_runs": [],
        }
    return cells


def _mean(total: float, n: int) -> float:
    return total / n if n else 0.0


# =============================================================================
# Pricing (Sonnet 4.6 list rates applied uniformly)
# =============================================================================


def _sonnet_priced(tokens: dict[str, float]) -> dict[str, float]:
    """Return per-class dollar contribution at Sonnet 4.6 list rates."""
    return {
        "input": tokens.get("input_tokens", 0.0) / MTOK * SONNET_INPUT_USD_PER_MTOK,
        "output": tokens.get("output_tokens", 0.0) / MTOK * SONNET_OUTPUT_USD_PER_MTOK,
        "cache_read": (
            tokens.get("cache_read_tokens", 0.0) / MTOK * SONNET_CACHE_READ_USD_PER_MTOK
        ),
        "cache_create": (
            tokens.get("cache_create_tokens", 0.0)
            / MTOK
            * SONNET_CACHE_CREATE_USD_PER_MTOK
        ),
    }


BUCKET_LABEL: dict[str, str] = {
    "flat_cli_tokens_consumed": "tokens_consumed (flat CLI)",
    "tokens_consumed": "tokens_consumed (BOSS + condense)",
    "worker_cost_recorded": "worker_cost_recorded (WORKER)",
}


# =============================================================================
# Rendering
# =============================================================================


def _fmt_tokens(value: float) -> str:
    if value < 1:
        return "0"
    return f"{value:,.0f}"


def _fmt_dollars(value: float) -> str:
    return f"${value:,.3f}"


def compose_bucket_table(cells: dict[str, dict[str, Any]]) -> str:
    """Per-cell, per-event-family row with token means + priced $ per class."""
    lines = [
        "| Cell | Event family | input_tok | output_tok | cache_read_tok | "
        "cache_create_tok | $ input | $ output | $ cache_read | $ cache_create | "
        "$ priced total | $ reported | dominant class |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]

    def _emit_row(cell: str, bname: str, tokens: dict[str, float], n: int) -> str:
        tok_mean = {
            k: _mean(tokens.get(k, 0.0), n)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_create_tokens",
            )
        }
        cost = _sonnet_priced(tok_mean)
        priced_total = sum(cost.values())
        reported_mean = _mean(tokens.get("reported_cost_usd", 0.0), n)
        dominant = max(cost, key=lambda k: cost[k]) if priced_total > 0 else "-"
        return (
            f"| {cell} | {BUCKET_LABEL[bname]} | "
            f"{_fmt_tokens(tok_mean['input_tokens'])} | "
            f"{_fmt_tokens(tok_mean['output_tokens'])} | "
            f"{_fmt_tokens(tok_mean['cache_read_tokens'])} | "
            f"{_fmt_tokens(tok_mean['cache_create_tokens'])} | "
            f"{_fmt_dollars(cost['input'])} | "
            f"{_fmt_dollars(cost['output'])} | "
            f"{_fmt_dollars(cost['cache_read'])} | "
            f"{_fmt_dollars(cost['cache_create'])} | "
            f"{_fmt_dollars(priced_total)} | "
            f"{_fmt_dollars(reported_mean)} | "
            f"{dominant} |"
        )

    for cell in ("A1", "A2"):
        c = cells[cell]
        for bname, tokens in c["buckets"].items():
            lines.append(_emit_row(cell, bname, tokens, c["n"]))

    for cell in ("B1", "B2"):
        c = cells[cell]
        for bname in ("worker_cost_recorded", "tokens_consumed"):
            lines.append(_emit_row(cell, bname, c["buckets"][bname], c["n"]))
    return "\n".join(lines)


def compose_cell_totals_table(cells: dict[str, dict[str, Any]]) -> str:
    """Per-cell totals summed across event families -- matches Section 4."""
    lines = [
        "| Cell | input_tok | output_tok | cache_read_tok | cache_create_tok | "
        "$ input | $ output | $ cache_read | $ cache_create | $ priced total | "
        "$ reported |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cell in ("A1", "A2", "B1", "B2"):
        c = cells[cell]
        n = c["n"]
        agg_tok = dict.fromkeys(
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_create_tokens"),
            0.0,
        )
        reported_total = 0.0
        for tokens in c["buckets"].values():
            for key in agg_tok:
                agg_tok[key] += tokens.get(key, 0.0)
            reported_total += tokens.get("reported_cost_usd", 0.0)
        tok_mean = {k: _mean(v, n) for k, v in agg_tok.items()}
        cost = _sonnet_priced(tok_mean)
        priced_total = sum(cost.values())
        lines.append(
            f"| {cell} | "
            f"{_fmt_tokens(tok_mean['input_tokens'])} | "
            f"{_fmt_tokens(tok_mean['output_tokens'])} | "
            f"{_fmt_tokens(tok_mean['cache_read_tokens'])} | "
            f"{_fmt_tokens(tok_mean['cache_create_tokens'])} | "
            f"{_fmt_dollars(cost['input'])} | "
            f"{_fmt_dollars(cost['output'])} | "
            f"{_fmt_dollars(cost['cache_read'])} | "
            f"{_fmt_dollars(cost['cache_create'])} | "
            f"{_fmt_dollars(priced_total)} | "
            f"{_fmt_dollars(_mean(reported_total, n))} |"
        )
    return "\n".join(lines)


def compose_by_model_diagnostic(cells: dict[str, dict[str, Any]]) -> str:
    """Diagnostic: split B ``tokens_consumed`` by model.

    Still priced at Sonnet 4.6 rates (so reviewers see exactly what the
    prompt's yardstick implies). The accompanying $ reported column
    shows the per-model actuals LiteLLM booked.
    """
    lines = [
        "| Cell | Model | input_tok | output_tok | cache_read_tok | "
        "cache_create_tok | $ priced (Sonnet rates) | $ reported (per-model) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cell in ("B1", "B2"):
        c = cells[cell]
        n = c["n"]
        for model, tokens in sorted(c["tokens_consumed_by_model"].items()):
            tok_mean = {
                k: _mean(tokens.get(k, 0.0), n)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_create_tokens",
                )
            }
            cost = _sonnet_priced(tok_mean)
            priced_total = sum(cost.values())
            reported_mean = _mean(tokens.get("reported_cost_usd", 0.0), n)
            lines.append(
                f"| {cell} | {model} | "
                f"{_fmt_tokens(tok_mean['input_tokens'])} | "
                f"{_fmt_tokens(tok_mean['output_tokens'])} | "
                f"{_fmt_tokens(tok_mean['cache_read_tokens'])} | "
                f"{_fmt_tokens(tok_mean['cache_create_tokens'])} | "
                f"{_fmt_dollars(priced_total)} | "
                f"{_fmt_dollars(reported_mean)} |"
            )
    return "\n".join(lines)


def compose_rationale(cells: dict[str, dict[str, Any]]) -> str:
    """Mechanism paragraph grounded in the per-class $ contribution."""

    def _cell_cost(cell: str) -> dict[str, float]:
        c = cells[cell]
        n = c["n"]
        agg = dict.fromkeys(
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_create_tokens"),
            0.0,
        )
        for tokens in c["buckets"].values():
            for key in agg:
                agg[key] += tokens.get(key, 0.0)
        tok_mean = {k: _mean(v, n) for k, v in agg.items()}
        return _sonnet_priced(tok_mean)

    a1, a2 = _cell_cost("A1"), _cell_cost("A2")
    b1, b2 = _cell_cost("B1"), _cell_cost("B2")

    return (
        "Per-class dollar contribution (Sonnet 4.6 list rates, N=10 mean): "
        f"**A1** cache_read ${a1['cache_read']:.2f} + output ${a1['output']:.2f} "
        f"(input ${a1['input']:.3f}, cache_create ${a1['cache_create']:.2f}); "
        f"**A2** cache_read ${a2['cache_read']:.2f} + output ${a2['output']:.2f}; "
        f"**B1** cache_read ${b1['cache_read']:.2f} + input ${b1['input']:.2f} + "
        f"cache_create ${b1['cache_create']:.2f} + output ${b1['output']:.2f}; "
        f"**B2** cache_read ${b2['cache_read']:.2f} + input ${b2['input']:.2f} + "
        f"cache_create ${b2['cache_create']:.2f}. "
        "A cells spend primarily on `cache_read` (a single long Sonnet "
        "session replays a growing prompt cache) and `output` (one "
        "large monolithic completion). B cells shift spend toward "
        "`input` (BOSS decomposition emits many bare prompts -- "
        "`tokens_consumed` captures Opus 4.7 BOSS calls; gpt-4o-mini "
        "condense is numerically negligible) while still paying heavy "
        "`cache_read` on the WORKER track (each child claude_code "
        "session replays its own cached prompt). Different mix, same "
        "$4-$6 band because cache_read remains the common anchor."
    )


# =============================================================================
# Entrypoint
# =============================================================================


def main() -> None:
    cells = build_per_cell()

    print("# Token / cost asymmetry across A1, A2, B1, B2\n")
    print("## Rate table (Sonnet 4.6 list rates, USD per MTok)\n")
    print(
        f"- input: ${SONNET_INPUT_USD_PER_MTOK:.2f}\n"
        f"- output: ${SONNET_OUTPUT_USD_PER_MTOK:.2f}\n"
        f"- cache_read: ${SONNET_CACHE_READ_USD_PER_MTOK:.2f}\n"
        f"- cache_create: ${SONNET_CACHE_CREATE_USD_PER_MTOK:.2f}\n"
    )
    print("## Per-cell, per-event-family token means and priced $ per class (N=10)\n")
    print(compose_bucket_table(cells))
    print()
    print("## Per-cell combined totals (sums the rows above)\n")
    print(compose_cell_totals_table(cells))
    print()
    print("## Diagnostic: B-cell ``tokens_consumed`` split by model\n")
    print(compose_by_model_diagnostic(cells))
    print()
    print("## Rationale\n")
    print(compose_rationale(cells))
    print()
    print("## Sanity check -- event sum vs INDEX\n")
    for cell in ("B1", "B2"):
        c = cells[cell]
        n = c["n"]
        reported_sum = sum(
            b.get("reported_cost_usd", 0.0) for b in c["buckets"].values()
        )
        idx_sum = c["index_total_sum"]
        print(
            f"- {cell}: event-derived mean ${_mean(reported_sum, n):.3f} "
            f"vs INDEX cost_breakdown.total_usd mean ${_mean(idx_sum, n):.3f} "
            f"(delta ${_mean(reported_sum - idx_sum, n):.4f})"
        )
    for cell in ("A1", "A2"):
        missing = cells[cell].get("missing_runs") or []
        if missing:
            print(
                f"- {cell}: {len(missing)} run(s) emitted zero tokens_consumed "
                f"events (wallclock cap): {', '.join(missing)}"
            )


if __name__ == "__main__":
    main()
