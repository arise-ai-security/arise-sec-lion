"""Audit worker-node prompt-cache behaviour from existing events.jsonl.

Question: do successive workers on the same CVE get Anthropic prompt-cache
hits on the stable prefix (system prompt + tool schemas + repo CLAUDE.md
injected by Claude Code into the cwd), or does each worker pay fresh
cache-creation tokens independently?

Signals computed per ``worker_cost_recorded`` event:

* ``cache_read_ratio = cache_read / (cache_read + cache_write + prompt)``
  Fraction of the worker's total input that was served from Anthropic's
  prompt cache. Within a single worker session this is driven by
  self-caching across turns; across worker sessions, a high first-event
  ``cache_read`` would be the tell-tale sign of cross-session reuse.

* ``cache_write_per_worker``. If workers on the same CVE share a stable
  prefix, only worker #1 should pay large cache-creation tokens; workers
  #2+ should pay near-zero cache_write because the prefix is already in
  cache. Flat or rising cache_write across worker index = NO cross-session
  sharing.

* ``same_cve_time_delta_s``. Time between consecutive worker_cost_recorded
  events on the same CVE+cell. Anthropic cache TTL is 5 min; if the delta
  exceeds 300 s, cache would have expired regardless.

The script only reads dataset/runs/ — no LLM calls, no mutations.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "dataset" / "runs"


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _coerce_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: object) -> float:
    try:
        result = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    # JSON allows NaN via Python's default loader; reject it so medians
    # don't propagate NaN through the aggregates.
    if result != result:  # True iff NaN
        return 0.0
    return result


def collect_worker_events() -> list[dict]:
    """Flatten every worker_cost_recorded event across all B-cell runs.

    Worker indices are assigned in events.jsonl file order, which equals
    spawn/finish order when ``max_concurrent_workers == 1`` (v1 + v2
    configs). Under parallel concurrency the two orders can differ; the
    sibling-pair analysis therefore always anchors on ``worker_idx`` rather
    than ``occurred_at`` so the "worker #0 vs later" contrast is defined by
    spawn order, not finish order.
    """
    rows: list[dict] = []
    if not RUNS.exists():
        return rows
    for cve_dir in sorted(RUNS.iterdir()):
        if not cve_dir.is_dir():
            continue
        for cell in ("B1", "B2"):
            events_path = cve_dir / cell / "0" / "events.jsonl"
            if not events_path.exists():
                continue
            worker_idx = 0
            for line_no, line in enumerate(
                events_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"warn: {events_path}:{line_no} un-parseable JSON, skipping"
                    )
                    continue
                if ev.get("event_type") != "worker_cost_recorded":
                    continue
                occurred = _parse_iso(str(ev.get("occurred_at", "")))
                if occurred is None:
                    print(
                        f"warn: {events_path}:{line_no} missing/invalid occurred_at, skipping"
                    )
                    continue
                pl = ev.get("payload") or {}
                prompt = _coerce_int(pl.get("prompt_tokens"))
                cache_read = _coerce_int(pl.get("cache_read_tokens"))
                cache_write = _coerce_int(pl.get("cache_write_tokens"))
                completion = _coerce_int(pl.get("completion_tokens"))
                input_total = prompt + cache_read + cache_write
                rows.append(
                    {
                        "cve": cve_dir.name,
                        "cell": cell,
                        "worker_idx": worker_idx,
                        "agent_id": ev.get("agent_id"),
                        "occurred_at": occurred,
                        "model": pl.get("model"),
                        "tool_name": pl.get("tool_name"),
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                        "input_total": input_total,
                        "cache_read_ratio": (
                            cache_read / input_total if input_total else 0.0
                        ),
                        "cache_write_share": (
                            cache_write / input_total if input_total else 0.0
                        ),
                        "fresh_share": (prompt / input_total if input_total else 0.0),
                        "cost_usd": _coerce_float(pl.get("cost_usd")),
                        "duration_s": _coerce_float(pl.get("duration_seconds")),
                    }
                )
                worker_idx += 1
    return rows


def per_cell_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate cache metrics per B1/B2 cell."""
    out = (
        df.groupby("cell")
        .agg(
            n_workers=("cve", "count"),  # count by cve -- always non-null
            n_cves=("cve", "nunique"),
            cost_usd_sum=("cost_usd", "sum"),
            prompt_tok_median=("prompt_tokens", "median"),
            cache_read_median=("cache_read_tokens", "median"),
            cache_write_median=("cache_write_tokens", "median"),
            cache_read_ratio_mean=("cache_read_ratio", "mean"),
            cache_read_ratio_median=("cache_read_ratio", "median"),
            cache_write_share_median=("cache_write_share", "median"),
        )
        .round(3)
    )
    return out


def sibling_pair_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """For each (CVE, cell) with >=2 workers, compare worker_idx=0 vs later.

    Anchors on ``worker_idx`` (spawn order, assigned at collection time in
    events.jsonl file order), NOT ``occurred_at`` (finish order). Under
    ``max_concurrent_workers=1`` these coincide, but ``worker_idx`` is the
    semantically correct axis regardless of concurrency.

    Signals:

    * ``worker0_cache_write`` vs ``later_cache_write_median``: if the
      stable prefix (system + tool schemas + CLAUDE.md) is shared across
      SDK sessions, only worker #0 should pay the one-time prefix cache
      creation; workers #1+ should pay substantially less. Similar values
      => cross-session cache NOT working.
    * ``max_consecutive_gap_s``: largest gap between worker #N and worker
      #N+1 end timestamps. If any consecutive gap exceeds 300 s the
      Anthropic cache TTL would have expired between workers regardless.
    """
    rows: list[dict] = []
    for (cve, cell), sub in df.groupby(["cve", "cell"]):
        if len(sub) < 2:
            continue
        sub = sub.sort_values("worker_idx").reset_index(drop=True)
        first = sub.iloc[0]
        later = sub.iloc[1:]
        timestamps = list(sub["occurred_at"])
        consecutive_gaps_s = [
            (timestamps[i + 1] - timestamps[i]).total_seconds()
            for i in range(len(timestamps) - 1)
        ]
        rows.append(
            {
                "cve": cve,
                "cell": cell,
                "n_workers": len(sub),
                "worker0_cache_write": int(first["cache_write_tokens"]),
                "later_cache_write_median": int(later["cache_write_tokens"].median()),
                "worker0_cache_read": int(first["cache_read_tokens"]),
                "later_cache_read_median": int(later["cache_read_tokens"].median()),
                "worker0_prompt_tok": int(first["prompt_tokens"]),
                "later_prompt_tok_median": int(later["prompt_tokens"].median()),
                "max_consecutive_gap_s": int(max(consecutive_gaps_s)),
                "all_gaps_within_5min": all(g <= 300 for g in consecutive_gaps_s),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    rows = collect_worker_events()
    if not rows:
        print("No worker_cost_recorded events found.")
        return

    df = pd.DataFrame(rows)

    print("## Per-worker raw view (first 40 rows)\n")
    cols = [
        "cve", "cell", "worker_idx",
        "prompt_tokens", "completion_tokens",
        "cache_read_tokens", "cache_write_tokens",
        "cache_read_ratio", "cache_write_share", "fresh_share",
        "cost_usd", "duration_s",
    ]
    print(df[cols].head(40).to_string(index=False))

    print("\n## Per-cell aggregate\n")
    print(per_cell_summary(df).to_string())

    print("\n## Sibling-pair analysis (CVEs with >=2 workers)\n")
    sibs = sibling_pair_analysis(df)
    if sibs.empty:
        print("No run had >=2 workers.")
    else:
        print(sibs.to_string(index=False))

    print("\n## Headline interpretation\n")
    sibs = sibling_pair_analysis(df)
    if sibs.empty:
        print("(insufficient multi-worker data to judge cross-session cache)")
        return
    w0 = sibs["worker0_cache_write"].median()
    wN = sibs["later_cache_write_median"].median()
    within_5min_share = sibs["all_gaps_within_5min"].mean()
    ratio_mean = df["cache_read_ratio"].mean()
    print(
        f"- median cache_write tokens -- worker#0: {w0:,.0f}  vs  worker#1+ median: {wN:,.0f}"
    )
    print(f"- share of sibling sequences with every gap within 5 min: {within_5min_share:.0%}")
    print(f"- overall per-worker cache_read ratio (mean): {ratio_mean:.1%}")
    if wN > 0.5 * w0:
        print(
            "\nFinding: later workers pay a similar cache_write to the first worker, "
            "so cross-session cache sharing is NOT happening. Each worker is a fresh "
            "Anthropic cache identity despite identical cwd / CLAUDE.md / tool schemas."
        )
    else:
        print(
            "\nFinding: later workers pay substantially less cache_write than the first worker. "
            "Cross-session cache sharing appears to be working."
        )


if __name__ == "__main__":
    main()
