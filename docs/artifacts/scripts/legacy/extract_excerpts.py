"""Produce 4 narrated log excerpts for docs/pillar_b/examples/.

Selections:
- tree_success_best.md   — best-performing B-cell run (mruby B1: build+exploit pass).
- tree_failure.md        — typical B2 failure (decomposition JSON error).
- flat_success_best.md   — A-cell run with most mechanical signal (mruby A1: builder pass).
- flat_failure.md        — typical A-cell failure (njs.cve-2022-32414 A1: budget blown, no patch).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS = REPO_ROOT / "dataset" / "runs"
EXAMPLES = REPO_ROOT / "docs" / "pillar_b" / "examples"
EXAMPLES.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("excerpts")


def read_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def summarize(event: dict, max_chars: int = 220) -> str:
    """Produce a single-line human-readable summary of an event."""
    et = event["event_type"]
    pl = event.get("payload") or {}
    seq = event.get("sequence_number", "?")
    role = event.get("role", "?")
    label = f"#{seq:>3} [{role:<7}] {et}"
    if et == "prompt_sent":
        pt = pl.get("prompt_text", "")
        label += f"  type={pl.get('prompt_type','?')} len={len(pt)}"
    elif et == "tokens_consumed":
        label += (
            f"  in={pl.get('input_tokens','?')} out={pl.get('output_tokens','?')} "
            f"cache_r={pl.get('cache_read_input_tokens',0)} "
            f"cost=${pl.get('cost_usd','?')} op={pl.get('operation','?')}"
        )
    elif et == "tool_use":
        data = pl.get("data")
        if isinstance(data, dict) and data.get("domain_event_type"):
            label += f"  domain={data['domain_event_type']}"
        else:
            tn = pl.get("tool_name", "?")
            ti = pl.get("tool_input") or {}
            if tn == "Bash":
                cmd = ti.get("command", "")[:80]
                label += f"  {tn}  $ {cmd}"
            elif tn in ("Read", "Edit", "Write"):
                fp = ti.get("file_path", "")
                label += f"  {tn}  file={fp}"
            elif tn in ("Grep", "Glob"):
                label += f"  {tn}  pattern={ti.get('pattern','?')}"
            else:
                label += f"  {tn}"
    elif et == "tool_result":
        data = pl.get("data")
        if isinstance(data, dict) and data.get("domain_event_type"):
            det = data["domain_event_type"]
            fields = data.get("fields", {})
            extras = ""
            if det == "OperationFinished":
                extras = f"  op={fields.get('operation_type','?')} dur={fields.get('duration_seconds',0):.1f}s"
            if det == "SubtasksDefined":
                extras = f"  n={fields.get('subtask_count',0)}"
            if det == "WorkFailed":
                extras = f"  reason={(fields.get('reason','') or '')[:80]}"
            label += f"  domain={det}{extras}"
        else:
            label += "  (tool_result text omitted)"
    elif et == "run_completed":
        label += f"  status={pl.get('status','?')} dur={pl.get('duration_seconds',0):.1f}s"
    elif et == "run_started":
        pass
    elif et == "agent_created":
        label += f"  task={(pl.get('task_description','') or '')[:80]}"
    text = label[:max_chars]
    return text


def emit_excerpt(
    title: str,
    narration: str,
    run_dir: Path,
    out_path: Path,
    *,
    start: int = 0,
    end: int | None = None,
    window_around_seq: tuple[int, int] | None = None,
) -> None:
    events = read_events(run_dir)
    if window_around_seq is not None:
        tgt, radius = window_around_seq
        events = [e for e in events if abs(e["sequence_number"] - tgt) <= radius]
    else:
        if end is None:
            end = len(events)
        events = events[start:end]
    lines = [f"# {title}", "", narration, "", "```", f"# run_dir: {run_dir.relative_to(REPO_ROOT)}"]
    for e in events:
        lines.append(summarize(e))
    lines.append("```")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d events)", out_path.name, len(events))


def main() -> int:
    # 1) Tree "best" run — mruby.cve-2022-0240 B1 (builder+exploiter pass;
    #    patch applied but did not fix SEGV).
    emit_excerpt(
        title="Tree (B1) best case — mruby.cve-2022-0240",
        narration=(
            "The boss decomposed the task, spawned two workers (build + patch), "
            "and the patch worker wrote `fix.patch`. The retrofitted mechanical "
            "evaluator applied the patch successfully (patch_exit=0) but "
            "`secb repro` still triggered the expected SEGV after the patch, "
            "so `fixer_pass` = False. This is the only B-cell run that passed both "
            "`builder_pass` and `exploiter_pass` — yet the model's patch did not "
            "actually suppress the sanitizer error."
        ),
        run_dir=RUNS / "mruby.cve-2022-0240" / "B1" / "0",
        out_path=EXAMPLES / "tree_success_best.md",
        start=0, end=35,
    )

    # 2) Tree failure — B2 njs decomposition failure
    emit_excerpt(
        title="Tree (B2) failure — decomposition JSON parse error",
        narration=(
            "Eleven of the ten B2 runs (including this one) failed at the BOSS "
            "decomposition stage because the LLM's response was not valid JSON. "
            "No worker agents were ever spawned, no patch was written, and the "
            "whole run ended in ~80 seconds at a cost of < $0.50 — the lowest "
            "per-run cost in the entire dataset, but zero progress. "
            "This is the dominant failure mode for B2."
        ),
        run_dir=RUNS / "njs.cve-2022-32414" / "B2" / "0",
        out_path=EXAMPLES / "tree_failure.md",
    )

    # 3) Flat success — A1 mruby builder-pass case
    emit_excerpt(
        title="Flat CLI (A1) — mruby.cve-2022-0240 long exploration",
        narration=(
            "The flat CLI (Claude Code, subagents ON) spent ~33 minutes and $5.57 "
            "on this CVE. It fired 124 Bash calls, 40 Reads, 6 Greps, 4 Globs, "
            "3 Writes, 3 Edits, 3 WebSearches, 2 Task subagents, and 1 ToolSearch. "
            "Excerpt shows the first 40 tool calls — notice repeated reads of the "
            "same files (e.g. `/src/CLAUDE.md` investigated multiple times, "
            "`ls /src` repeated) illustrating the 35 %% intra-agent redundancy we "
            "measured across A1. No patch reached workspace/; builder_pass=True was "
            "purely the image's pristine build succeeding."
        ),
        run_dir=RUNS / "mruby.cve-2022-0240" / "A1" / "0",
        out_path=EXAMPLES / "flat_success_best.md",
        start=0, end=50,
    )

    # 4) Flat failure — large, expensive, no artifact
    emit_excerpt(
        title="Flat CLI (A1) — njs.cve-2022-32414 budget burn, no patch",
        narration=(
            "A 499-event run that spent ~33 minutes and $6.66, making 248 tool "
            "calls. The agent explored heavily but produced no model_patch.diff "
            "in workspace/ and no repro.sh either — mechanical_pass is all False "
            "both pre- and post-retrofit. Excerpt shows the last ~30 events: the "
            "final tokens_consumed and run_completed indicate the agent simply "
            "stopped producing without declaring completion."
        ),
        run_dir=RUNS / "njs.cve-2022-32414" / "A1" / "0",
        out_path=EXAMPLES / "flat_failure.md",
        start=-30,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
