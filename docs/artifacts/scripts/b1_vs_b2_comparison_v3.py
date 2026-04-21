"""B1-vs-B2 comparison on the v3 rerun dataset.

Why this script exists
======================
Pillar B of the tree-vs-flat experiment has two cells:

* ``B1`` -- tree orchestration, NO domain briefing (``NullPromptStrategy``).
* ``B2`` -- tree orchestration, WITH the SEC-bench domain briefing injected
  (``SecBenchPromptStrategy``, ~25.3 KB Jinja-rendered preamble).

Everything else is identical between the two cells: same worker model, same
Docker image, same budget/wallclock cap, same 10 CVEs. So the B1-vs-B2 delta
isolates the effect of the briefing itself -- which is pre-registered as
comparison S1 in ``experiments/configs/pre_registration.yaml``.

The v2 dataset (dataset-v2-20260420) could only partially answer S1 because
the Claude Agent SDK adapter at the time emitted every ``tool_use`` event
with ``tool_name="Tool"`` and ``tool_input={}``. That made several metrics
literally zero-by-construction for B cells:

    per-tool breakdown, security_tool_adoption, redundancy_{intra,sibling,
    hierarchy}, audit_violations.

The v3 rerun (dataset-v3-rerun) applies the adapter fix from
``infrastructure/adapters/worker/claude_sdk_adapter.py`` that now forwards
``input_data["tool_name"]`` and ``input_data["tool_input"]``. This is
verified empirically: a sample B1 ``tool_use`` event now has
``tool_name="Bash"`` with ``tool_input={command, description}``. Therefore
all previously artefactual zeros become real measurements in v3, and this
script foregrounds them.

Metrics compared (justification per group)
==========================================

Primary outcome (the experiment's pre-registered question)
----------------------------------------------------------
* ``mech_end_to_end`` -- paired McNemar -> S1 pre-registered test.
* ``mech_builder`` / ``mech_exploiter`` / ``mech_fixer`` -- per-phase paired
  McNemar. Important because v2 already showed 0/10 end-to-end everywhere;
  looking at individual phases shows where the briefing helped (or not).
* Deliverable on disk (patch / report / poc). REPORT.md §6 "Phase Reached"
  called this out as the ONLY discriminating signal in v2, so we replicate it.

Cost-of-briefing (the mechanism of the treatment)
-------------------------------------------------
These reveal WHAT the 25.3 KB briefing actually does to the session:

* ``total_cost_usd`` -- dollar footprint.
* ``cache_read_input_tokens`` -- the one REPORT §5 said dominates. A larger
  preamble means larger cache replay per turn, compounding over many turns.
* ``input_tokens`` -- fresh prompt content. B2 should have higher first-turn
  input since it ships the briefing as part of each BOSS prompt; beyond that,
  caching takes over so this bucket stays small.
* ``output_tokens`` -- model-generated tokens. A proxy for how much the
  briefing steers the model into producing (reports, markdown, structure).
* ``cache_creation_input_tokens`` -- new prefix committed to cache; spikes
  when the shared preamble first enters cache.
* ``wallclock_seconds`` + ``event_count`` -- time/work proxies.

Behavioural consequences (newly measurable in v3)
-------------------------------------------------
Each of these was zero-by-construction in v2; non-zero values here are the
first time we can say anything empirical about B-cell behaviour.

* ``tool_calls_total`` and per-tool counts ({Bash, Read, Edit, Write, Grep,
  Glob, TodoWrite, ...}). Answers: does the briefing push the model toward
  different tooling? e.g. more ``Read`` for investigation, more ``Write``
  for reports.
* ``security_tool_adoption`` -- does any ``Bash`` call run ``valgrind`` or
  ``klee``? The briefing explicitly prescribes these; if the briefing works,
  B2 should have more adopters than B1.
* ``security_tool_invocations`` -- count of those calls.
* Redundancy triad (``intra`` / ``sibling`` / ``hierarchy``) and the rolled-up
  rate. REPORT §3.3 + §9 argue briefing should INCREASE sibling redundancy
  (identical 25.3 KB preamble in every sibling worker's prompt), which in
  turn explains the cache_read dominance in §5. We measure the rate directly
  at the (tool, target) level -- the pre-registered R1 metric.
* Audit violations by type (git_log_all, webfetch_external, ...). Does the
  briefing's "anti-cheat" clause suppress or fail to suppress violations?

Prompt redundancy (the most-discriminating signal in v2)
--------------------------------------------------------
* Sibling first-prompt Jaccard (tiktoken cl100k_base token sets). REPORT §9
  measured 0.872 in B1 vs 0.946 in B2 -- the clearest B2-specific signature.
  The briefing is a ~25 KB block that is, by construction, identical across
  siblings, so B2 Jaccard should be near-unity. Confirming this here is the
  prompt-level root cause of the cache_read pattern.
* Hierarchy first-prompt Jaccard (nearest prompted ancestor vs descendant).
  Expected to be moderate since the ancestor's context leaks downward via
  ``parent_context``.
* CNR (cumulative novelty ratio) per run (mean across the run's
  ``prompt_sent`` events). If every new prompt is mostly re-serialized
  preamble, CNR drops. Complementary to Jaccard.

Process integrity
-----------------
* ``termination_reason`` distribution. In v3 the wallclock cap was tightened
  from 5400 s to 2700 s (see ``meta.json.wallclock_sec_cap``); this forces a
  meaningful completion-rate comparison -- B2 completing less often than B1
  under the same cap is itself a headline finding worth reporting.

Design choices (with rationale)
===============================

1. **INDEX deduplication policy: last-row-wins by ``run_id``.** ``INDEX.jsonl``
   is append-only and contains duplicate entries for runs that were retried
   after anomaly terminations. Only one physical run directory exists per
   (cve, cell, replicate) and its ``events.jsonl``/``meta.json`` reflect the
   FINAL attempt. The last-wins policy keeps the INDEX consistent with the
   on-disk state. (The common ``dict.setdefault`` pattern would lock in the
   stale first row instead -- do not copy it here.)

2. **Pairing key: ``cve_id`` only.** The v3 rerun has ``replicate=0`` for
   every run (all CVEs, both cells). If future replicates are added, the key
   should extend to ``(cve_id, replicate)``; this is flagged in code.

3. **Inclusion of anomaly-terminated runs.** These ARE the experimental
   result: B2 hitting wallclock is a consequence of the briefing, not a
   measurement error. Excluding them would bias the comparison toward
   "behaviour conditional on completion", which is not what S1 asks. We
   therefore include all 20 runs in the primary paired tests AND also report
   a complete-only sensitivity.

4. **Self-contained file.** Helpers from ``compute_metrics.py`` and
   ``b_prompt_redundancy.py`` are duplicated rather than imported so the
   script runs with a clean ``PYTHONPATH`` from anywhere under the repo and
   so downstream scripts can not silently repoint at the v3 dataset by
   importing this module.

5. **No execution side-effects at import.** All IO happens in ``main()``.

6. **Deliverable detection ignores ``llvmsymbol.diff``.** That file is a
   stock artefact injected by the SEC-bench Docker image (``src/llvmsymbol.diff``
   appears in every run regardless of agent activity). Counting it would
   inflate the "produced a patch" signal to 100% for both cells.

Inputs
------
Read-only, all paths relative to the merged ``dataset-final/`` tree:

* ``dataset-final/INDEX.jsonl`` -- journal of run summaries (includes cost,
  termination_reason, mechanical_pass, artefacts, audit_violations). Contains
  A1 + A2 rows from v1 and B1 + B2 rows from the v3 rerun; this script only
  pairs the B cells but the A rows coexist in the same file.
* ``dataset-final/runs/{cve_id}/{cell}/{replicate}/events.jsonl`` -- per-run
  event log. B1/B2 subdirs are hardlinks into ``dataset-v3-rerun/runs/``.
* ``dataset-final/runs/{cve_id}/{cell}/{replicate}/meta.json`` -- config
  snapshot including ``wallclock_sec_cap`` and ``prompt_strategy``.
* ``dataset-final/runs/{cve_id}/{cell}/{replicate}/mechanical.json`` --
  canonical evaluator result (authoritative -- INDEX copy may lag).
* ``dataset-final/runs/{cve_id}/{cell}/{replicate}/audit.json`` --
  ``audit_cheating.py`` output.
* ``dataset-final/runs/{cve_id}/{cell}/{replicate}/<agent_uuid>/testcase/`` --
  where agents write produced artefacts.

Outputs (under ``dataset-final/tables/v3_b1_vs_b2/``)
------------------------------------------------------
* ``per_cve.csv`` -- one row per CVE, all metrics as ``<metric>_B1`` /
  ``<metric>_B2`` / ``<metric>_delta`` columns.
* ``summary.csv`` -- per-cell aggregates (mean, median, min, max, count) and
  paired-stat results (McNemar p, Wilcoxon p, diff, CI).
* ``termination.csv`` -- cross-tab of termination_reason x cell.
* ``tool_breakdown.csv`` -- per-tool totals per cell, plus per-cell share.
* ``audit_breakdown.csv`` -- violation counts by type per cell.
* ``prompt_redundancy.csv`` -- every sibling/hierarchy Jaccard pair, tagged
  with cve_id, cell, and scope (``worker_only`` = v2-comparable, ``full_tree``
  = includes v3 MANAGER prompts), plus per-cell summary rows.

Not written on import: the script is a module. Call ``main()`` (or run as a
script) to produce the outputs.

Run
---
This script is intentionally NOT executed yet. When you are ready:

    uv run python dataset-final/scripts/b1_vs_b2_comparison_v3.py

Review gate
-----------
Per repo ``CLAUDE.md`` rule 4, the numbers this script produces must be
reviewed by ``codex:codex-rescue`` before being quoted in any report. The
code itself is review-gated under rule 6 before the task that created it
can be marked complete.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import tiktoken


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

# Script lives at ``docs/artifacts/scripts/<this>.py`` (the repo-wide
# convention, REPORT.md Appendix C). ``parents[3]`` = repo root. The merged
# dataset lives at ``<repo>/dataset-final/`` -- A1 + A2 runs hardlinked from
# v1 ``datasets-legacy/dataset/``; B1 + B2 runs hardlinked from
# ``datasets-legacy/dataset-v3-rerun/`` with last-wins dedup by ``run_id``.
REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ROOT = REPO_ROOT / "dataset-final"
RUNS_ROOT = DATASET_ROOT / "runs"
INDEX_PATH = DATASET_ROOT / "INDEX.jsonl"
# Output tables land under ``docs/artifacts/tables/`` to keep raw data and
# derived artefacts separated. Matches the existing v1/v2 convention
# (``comparison_results.csv``, ``index_enriched.csv`` already live there).
OUT_DIR = REPO_ROOT / "docs" / "artifacts" / "tables" / "v3_b1_vs_b2"

# The two cells being compared. ``CELL_X`` is the baseline ("did NOT receive the
# briefing"), ``CELL_Y`` is the treatment. McNemar/Wilcoxon sign conventions
# below assume X = baseline, Y = treatment, so the "diff" is treatment minus
# baseline.
CELL_X = "B1"
CELL_Y = "B2"
CELLS = (CELL_X, CELL_Y)

# Security tools the domain briefing prescribes (REPORT §2.1, §3.2).
# We detect adoption by substring match on the Bash ``command`` field because
# the agents can invoke them under many flags/paths.
SECURITY_TOOLS: tuple[str, ...] = ("valgrind", "klee")

# Deliverable detection patterns. Agents write under
# ``<run_dir>/<agent_uuid>/testcase/`` (and occasionally ``.../src/``). The
# regex approach (rather than glob) is a deliberate match with the v2
# reference analysis ``phase_reached.py`` -- keeping the same patterns lets
# v3 "phase reached" numbers be directly compared to REPORT §6 v2 numbers.

# Patch filenames: model_patch.diff, fix.patch, cve-*.patch, *_patch.diff.
# CRITICAL: ``repo_changes.diff`` is NOT counted as a patch. ``secb``
# auto-generates it during build customisation, so its presence does not
# signal fixer-phase work (see phase_reached.py:124-129 for rationale).
PATCH_NAME_RE: re.Pattern[str] = re.compile(
    r"^(?:model_patch\.diff|fix\.patch|cve-[0-9\-]+\.patch|patch\.diff|.*_patch\.diff)$",
    re.IGNORECASE,
)

# Reports: canonical ``security_report.md`` + common close variants. We
# accept any ``*report*.md`` so root_cause.md / validation_report.md /
# verification_report.md / fix_report.md all count -- consistent with
# v2_progress_analysis.py and observed v3 filenames.
REPORT_NAME_RE: re.Pattern[str] = re.compile(
    r"^(?:"
    r"security[_-]report\.md|"
    r"verification[_-]report\.md|"
    r"validation(?:[_-]report)?\.md|"
    r"fix[_-]report\.md|"
    r"repro[_-]report\.md|"
    r"final[_-]report\.md|"
    r"root[_-]cause\.md|"
    r"report\.md|"
    r"findings\.md"
    r")$",
    re.IGNORECASE,
)

# PoC / reproducer filenames (broader regex cloned from phase_reached.py:119
# so case-insensitive ``POC1``, bare ``poc``, ``repro.sh``, ``reproduce.sh``,
# ``crash_*`` and ``asan_*.log|txt`` are all captured). The narrow v2 glob
# list missed ``POC1`` and bare ``poc`` on v3 runs (see Codex review finding).
POC_NAME_RE: re.Pattern[str] = re.compile(
    r"^(?:"
    r"poc(?:[._].+)?|"
    r"repro(?:\.sh|duce\.sh)?|"
    r"crash(?:[_\-\.].+)?|"
    r"asan[_\-].*\.(?:log|txt)|"
    r"generate_poc\.py"
    r")$",
    re.IGNORECASE,
)

# Stock files produced by the SEC-bench Docker image, present regardless of
# agent behaviour. Must never be counted as produced deliverables.
#
# ``llvmsymbol.diff`` -- stock toolchain artefact under ``src/`` in every
# run (observed in all v3 runs).
# ``repo_changes.diff`` -- auto-generated by ``secb`` build customisation;
# treated as non-patch per phase_reached.py:124-129.
# ``base_commit_hash`` -- pre-staged bookkeeping file (phase_reached.py:114).
STOCK_FILE_NAMES: frozenset[str] = frozenset(
    {"llvmsymbol.diff", "repo_changes.diff", "base_commit_hash"}
)

# CNR / prompt redundancy tokeniser. cl100k_base is the same proxy used in the
# v2 analysis (``b_prompt_redundancy.py`` + REPORT §3.4); reusing it keeps
# numbers comparable across datasets.
TOKENISER_NAME = "cl100k_base"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("b1_vs_b2_v3")


# ---------------------------------------------------------------------------
# Event / file utilities
# ---------------------------------------------------------------------------

def _iter_events(run_dir: Path) -> Iterable[dict]:
    """Yield parsed events from a run's ``events.jsonl``; skip malformed lines.

    Robustness notes:
      - If ``events.jsonl`` is missing (broken run), silently yield nothing
        -- downstream code treats that as zero events, which is the honest
        measurement.
      - Individual malformed lines are skipped rather than failing the run,
        matching the convention in ``compute_metrics.py``.
    """
    path = run_dir / "events.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _target_from_tool_input(tool_name: str, tool_input: dict) -> str | None:
    """Normalise a tool call into a stable ``target`` string for redundancy.

    Same extraction logic as
    ``docs/artifacts/scripts/compute_metrics.py._target_from_tool_input`` --
    this is the pre-registered definition of a "(tool, target)" pair. Keeping
    it byte-identical means v3 redundancy counts are directly comparable to
    any v2-style numbers produced by the older script.

    Returns ``None`` when no sensible target exists (e.g. ``TodoWrite``),
    which excludes that call from redundancy accounting.
    """
    if not isinstance(tool_input, dict) or not tool_input:
        return None
    if tool_name in {"Read", "Edit", "Write"}:
        fp = tool_input.get("file_path")
        if isinstance(fp, str):
            return fp
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if isinstance(cmd, str):
            # First 3 whitespace-separated tokens: captures "git log --all",
            # "valgrind --tool=memcheck ./poc", etc. The choice of 3 matches
            # the v2 spec; changing it would break comparability.
            toks = cmd.strip().split()
            return " ".join(toks[:3])
    if tool_name in {"Grep", "Glob"}:
        pat = tool_input.get("pattern") or tool_input.get("query")
        if isinstance(pat, str):
            return pat
    return None


def _tokeniser() -> tiktoken.Encoding:
    """Return the cached cl100k_base encoding for prompt-level similarity."""
    return tiktoken.get_encoding(TOKENISER_NAME)


def _safe_encode(enc: tiktoken.Encoding, text: str) -> list[int]:
    """Tokenise while tolerating unexpected special tokens in prompt bodies."""
    try:
        return enc.encode(text, disallowed_special=())
    # tiktoken may raise on weird UTF-8 sequences; treat as "no tokens" rather
    # than failing the whole run.
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Per-run metric container
# ---------------------------------------------------------------------------

@dataclass
class RunMetrics:
    """Everything computed from a single run's ``events.jsonl``.

    Populated by ``compute_run_metrics``. Kept as a plain dataclass (not a
    frozen Pydantic model) because this is an analysis script; no domain
    invariants apply.
    """

    run_id: str

    # --- Token accounting -- TWO EVENT FAMILIES ---------------------------
    # v3 records tokens in two disjoint event families and getting this wrong
    # is the difference between "worker cache_read = 0" (the v2 artefact we
    # are trying NOT to re-introduce) and the real 7-12 M cache_read numbers
    # quoted in REPORT §5.
    #
    # (1) ``tokens_consumed`` -- emitted by the tree's own LLM calls:
    #     * BOSS (Opus 4.7) decomposition / re-decomposition
    #     * MANAGER (Sonnet 4.6) planning
    #     * Context-condense passes (gpt-4o-mini) both for BOSS/manager
    #       memory and per-worker pre-prompt trimming
    #     Payload fields: ``input_tokens``, ``output_tokens``,
    #     ``cache_read_input_tokens``, ``cache_creation_input_tokens``,
    #     ``thinking_tokens``, ``cost_usd``.
    #
    # (2) ``worker_cost_recorded`` -- emitted once per worker Sonnet session
    #     (wraps the Claude Agent SDK's own cost accounting for the whole
    #     multi-turn worker interaction). Payload fields differ:
    #     ``prompt_tokens``, ``completion_tokens``, ``cache_read_tokens``,
    #     ``cache_write_tokens``, ``cost_usd``, ``duration_seconds``.
    #     REPORT §5's "worker sessions" columns ($3-5 of the cell's total,
    #     7.8M-10.8M cache_read) are exactly these events; summing only
    #     ``tokens_consumed`` would miss them.
    #
    # We therefore keep the two families in separate buckets AND sum them
    # into combined ``*_total`` fields. Downstream CSVs should prefer the
    # ``*_total`` values when writing "per-run tokens", because that matches
    # REPORT §5's per-run numbers; the split buckets let anyone audit the
    # breakdown in the per-cve table.
    tc_input_tokens: int = 0
    tc_output_tokens: int = 0
    tc_cache_read_tokens: int = 0
    tc_cache_creation_tokens: int = 0
    tc_thinking_tokens: int = 0
    tc_cost_usd: float = 0.0

    wcr_input_tokens: int = 0
    wcr_output_tokens: int = 0
    wcr_cache_read_tokens: int = 0
    wcr_cache_creation_tokens: int = 0
    wcr_cost_usd: float = 0.0
    wcr_duration_seconds: float = 0.0

    # Per-role cost split -- lets us check whether the briefing's extra cost
    # falls on the BOSS (Opus), the MANAGER (Sonnet), or the WORKER (Sonnet
    # via SDK). Populated from ``tokens_consumed.role`` + a synthetic
    # ``"WORKER_SDK"`` bucket for ``worker_cost_recorded`` events.
    cost_by_role_usd: dict[str, float] = field(default_factory=dict)

    # Tool activity (only measurable because of the v3 SDK fix).
    tool_calls_total: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    security_tool_adoption: bool = False
    security_tool_invocations: int = 0

    # Redundancy triad (pre-registered R1).
    redundancy_intra: int = 0
    redundancy_sibling: int = 0
    redundancy_hierarchy: int = 0
    redundancy_rate_total: float | None = None

    # Agent topology (used for sibling/hierarchy redundancy AND Jaccard).
    agent_parent: dict[str, str | None] = field(default_factory=dict)
    agent_role: dict[str, str] = field(default_factory=dict)

    # Prompt-level redundancy (first prompt per agent).
    first_prompt_token_set: dict[str, set[int]] = field(default_factory=dict)

    # CNR across the run's prompt_sent events (mean of per-prompt novelty).
    cnr_mean: float | None = None
    cnr_n_prompts: int = 0

    # Event count (for cross-check against INDEX.event_count).
    event_count: int = 0


def compute_run_metrics(run_dir: Path, run_id: str) -> RunMetrics:
    """Single-pass parse of ``events.jsonl`` populating every event-derived metric.

    One pass is important: B2 runs routinely exceed 5k events and re-opening
    the file per metric would be wasteful. All statistics that can be derived
    from the event stream are aggregated here; cross-metric derived values
    (redundancy rate, CNR) are finalised after the loop.
    """
    rm = RunMetrics(run_id=run_id)
    enc = _tokeniser()

    # Per-agent (tool, target) call history, used for intra/sibling/hierarchy
    # redundancy. Keyed by agent_id (a UUID string); calls without a target
    # (e.g. TodoWrite) are excluded per _target_from_tool_input.
    per_agent_calls: dict[str, list[tuple[str, str]]] = defaultdict(list)

    # Per-agent ordered prompt texts -- we keep only the FIRST for Jaccard,
    # matching the v2 analysis's definition.
    per_agent_prompts: dict[str, list[str]] = defaultdict(list)

    # CNR accumulator. CNR_t = |novel_tokens_in_prompt_t| / |tokens_in_prompt_t|
    # where "novel" = tokens not seen in any earlier prompt. The run-level
    # number is the arithmetic mean across prompts.
    seen_tokens: set[int] = set()
    cnr_values: list[float] = []

    # Tool-name counter and security-tool scan (Bash command substring match).
    tool_counter: Counter[str] = Counter()
    security_hits = 0

    for ev in _iter_events(run_dir):
        rm.event_count += 1
        et = ev.get("event_type")
        payload: dict = ev.get("payload") or {}

        if et == "agent_created":
            # ``agent_id`` lives at the top level of the event, not inside
            # ``payload`` (verified via dataset-v3-rerun sample inspection).
            aid = ev.get("agent_id")
            if aid is None:
                continue
            rm.agent_parent[aid] = ev.get("parent_agent_id")
            # Role comes from the top-level field first (present in v3
            # events), with payload fallback for older layouts.
            rm.agent_role[aid] = ev.get("role") or payload.get("role") or "?"

        elif et == "prompt_sent":
            aid = ev.get("agent_id")
            text = payload.get("prompt_text") or ""
            if not text:
                continue
            # Collect per-agent for Jaccard (we will take the first below).
            if aid:
                per_agent_prompts[aid].append(text)
            # Update CNR from the full token sequence.
            tokens = _safe_encode(enc, text)
            tset = set(tokens)
            if not tset:
                continue
            novel = tset - seen_tokens
            cnr_values.append(len(novel) / len(tset))
            seen_tokens.update(tset)

        elif et == "tool_use":
            # Some tree-domain events are wrapped as tool_use with
            # ``payload.data`` holding the real shape. Those are not real
            # model-issued tool calls -- skip (same filter as compute_metrics.py).
            if isinstance(payload.get("data"), dict):
                continue

            tn = payload.get("tool_name")
            # Defensive: if the v3 rerun ever produced a stray
            # ``tool_name="Tool"`` (v2 artefact), we still skip so we do not
            # silently re-introduce the v2 measurement ceiling.
            if not tn or tn in {"?", "Tool"}:
                continue
            tool_counter[tn] += 1

            ti = payload.get("tool_input") or {}
            target = _target_from_tool_input(tn, ti)
            aid = ev.get("agent_id") or "FLAT"
            if target is not None:
                per_agent_calls[aid].append((tn, target))

            # Security-tool adoption: substring match on the Bash command.
            if tn == "Bash":
                cmd = ti.get("command", "")
                if isinstance(cmd, str) and any(t in cmd for t in SECURITY_TOOLS):
                    security_hits += 1

        elif et == "tokens_consumed":
            # Tree-issued LLM calls (BOSS / MANAGER / condense).
            rm.tc_input_tokens += int(payload.get("input_tokens") or 0)
            rm.tc_output_tokens += int(payload.get("output_tokens") or 0)
            rm.tc_cache_read_tokens += int(payload.get("cache_read_input_tokens") or 0)
            rm.tc_cache_creation_tokens += int(
                payload.get("cache_creation_input_tokens") or 0
            )
            rm.tc_thinking_tokens += int(payload.get("thinking_tokens") or 0)
            cost = float(payload.get("cost_usd") or 0.0)
            rm.tc_cost_usd += cost

            role = ev.get("role") or "?"
            rm.cost_by_role_usd[role] = rm.cost_by_role_usd.get(role, 0.0) + cost

        elif et == "worker_cost_recorded":
            # Worker Sonnet session cost (the Claude Agent SDK reports this
            # ONCE per worker, covering the whole multi-turn interaction).
            # Field names differ from ``tokens_consumed``: ``prompt_tokens``
            # maps to input, ``completion_tokens`` to output,
            # ``cache_read_tokens`` (no "_input_") to cache read,
            # ``cache_write_tokens`` to cache create.
            rm.wcr_input_tokens += int(payload.get("prompt_tokens") or 0)
            rm.wcr_output_tokens += int(payload.get("completion_tokens") or 0)
            rm.wcr_cache_read_tokens += int(payload.get("cache_read_tokens") or 0)
            rm.wcr_cache_creation_tokens += int(payload.get("cache_write_tokens") or 0)
            wcost = float(payload.get("cost_usd") or 0.0)
            rm.wcr_cost_usd += wcost
            rm.wcr_duration_seconds += float(payload.get("duration_seconds") or 0.0)
            # Tag as a synthetic "WORKER_SDK" role in the per-role split so
            # it is not silently merged with the (separate) tokens_consumed
            # WORKER rows that carry the gpt-4o-mini condense-pass cost.
            rm.cost_by_role_usd["WORKER_SDK"] = (
                rm.cost_by_role_usd.get("WORKER_SDK", 0.0) + wcost
            )

    # --- Finalise tool-derived counters -----------------------------------
    rm.tool_calls_by_name = dict(tool_counter)
    rm.tool_calls_total = sum(tool_counter.values())
    rm.security_tool_adoption = security_hits > 0
    rm.security_tool_invocations = security_hits

    # --- Finalise CNR -----------------------------------------------------
    rm.cnr_n_prompts = len(cnr_values)
    rm.cnr_mean = statistics.fmean(cnr_values) if cnr_values else None

    # --- Finalise redundancy (intra, sibling, hierarchy) ------------------
    # Intra: same agent issues the same (tool, target) more than once. Count
    # of "extra" issues = (count - 1) summed across duplicated pairs.
    intra = 0
    for calls in per_agent_calls.values():
        c = Counter(calls)
        intra += sum(v - 1 for v in c.values() if v > 1)
    rm.redundancy_intra = intra

    # Sibling: two+ children of the same parent each issue the same (tool,
    # target). For each group of siblings, we count one-less-than-children
    # for every duplicated pair -- matching compute_metrics.py.
    sibling = 0
    sibling_groups: dict[str | None, list[str]] = defaultdict(list)
    for aid, pid in rm.agent_parent.items():
        sibling_groups[pid].append(aid)
    for pid, kids in sibling_groups.items():
        if pid is None or len(kids) < 2:
            continue
        pair_members: dict[tuple[str, str], set[str]] = defaultdict(set)
        for c in kids:
            for pair in per_agent_calls.get(c, []):
                pair_members[pair].add(c)
        for members in pair_members.values():
            if len(members) >= 2:
                sibling += len(members) - 1
    rm.redundancy_sibling = sibling

    # Hierarchy: a descendant re-issues a (tool, target) that any ancestor
    # already issued. Implemented via ancestor set per agent (via
    # agent_parent chain) then a count of overlaps.
    def ancestors(aid: str) -> list[str]:
        out, cur = [], rm.agent_parent.get(aid)
        while cur is not None:
            out.append(cur)
            cur = rm.agent_parent.get(cur)
        return out

    hierarchy = 0
    for aid, calls in per_agent_calls.items():
        anc_pairs: set[tuple[str, str]] = set()
        for anc in ancestors(aid):
            anc_pairs.update(per_agent_calls.get(anc, []))
        for pair in calls:
            if pair in anc_pairs:
                hierarchy += 1
    rm.redundancy_hierarchy = hierarchy

    if rm.tool_calls_total:
        rm.redundancy_rate_total = (
            (intra + sibling + hierarchy) / rm.tool_calls_total
        )

    # --- Finalise Jaccard inputs (first-prompt token sets) ----------------
    for aid, prompts in per_agent_prompts.items():
        if not prompts:
            continue
        rm.first_prompt_token_set[aid] = set(_safe_encode(enc, prompts[0]))

    return rm


# ---------------------------------------------------------------------------
# Deliverable detection
# ---------------------------------------------------------------------------

def _iter_testcase_files(run_dir: Path) -> Iterable[Path]:
    """Yield every file under ``<run_dir>/<agent_uuid>/(testcase|src)/``.

    Agents write produced artefacts into per-agent ``testcase/`` (primary)
    and sometimes ``src/`` subdirectories. Any file found there is a
    candidate "produced" deliverable. Stock files from the Docker image
    (``llvmsymbol.diff``) are filtered out at classification time, not here.
    """
    if not run_dir.exists():
        return
    for agent_dir in run_dir.iterdir():
        # Agent dirs are UUIDs; skip non-dirs and the aggregated ``workspace``.
        if not agent_dir.is_dir() or agent_dir.name == "workspace":
            continue
        for sub in ("testcase", "src"):
            sub_dir = agent_dir / sub
            if not sub_dir.is_dir():
                continue
            for p in sub_dir.iterdir():
                if p.is_file():
                    yield p


def has_patch(run_dir: Path) -> bool:
    """True iff the run produced a genuine patch file under any agent dir.

    Accepts any filename matching ``PATCH_NAME_RE`` (case-insensitive):
    ``model_patch.diff``, ``fix.patch``, ``cve-*.patch``, ``*_patch.diff``,
    ``patch.diff``. Stock files (``llvmsymbol.diff``, ``repo_changes.diff``,
    ``base_commit_hash``) are filtered out first -- per phase_reached.py's
    convention, ``repo_changes.diff`` is treated as NON-patch because secb
    auto-generates it during build customisation.
    """
    for f in _iter_testcase_files(run_dir):
        if f.name in STOCK_FILE_NAMES:
            continue
        if PATCH_NAME_RE.match(f.name):
            return True
    return False


def has_report(run_dir: Path) -> bool:
    """True iff the run produced a markdown-style report under any agent dir.

    See ``REPORT_NAME_RE`` for the accepted name list. Accepts the canonical
    ``security_report.md`` and the variants observed in v3 runs.
    """
    for f in _iter_testcase_files(run_dir):
        if f.name in STOCK_FILE_NAMES:
            continue
        if REPORT_NAME_RE.match(f.name):
            return True
    return False


def has_poc(run_dir: Path) -> bool:
    """True iff the run produced a PoC-named artefact under any agent dir.

    Accepts any filename matching ``POC_NAME_RE`` (case-insensitive):
    ``poc``, ``POC1``, ``poc_<anything>``, ``poc.<ext>``, ``repro.sh``,
    ``reproduce.sh``, ``crash_*``, ``asan_*.log|txt``, ``generate_poc.py``.
    """
    for f in _iter_testcase_files(run_dir):
        if f.name in STOCK_FILE_NAMES:
            continue
        if POC_NAME_RE.match(f.name):
            return True
    return False


# ---------------------------------------------------------------------------
# Auxiliary file loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Read a JSON file if present; otherwise return an empty dict.

    Used for ``mechanical.json``, ``meta.json``, ``audit.json``. Missing file
    is not a fatal error -- we degrade gracefully so one broken run cannot
    poison the whole comparison.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Malformed JSON at %s -- treating as empty", path)
        return {}


def load_index() -> dict[str, dict]:
    """Load ``INDEX.jsonl`` deduplicated by ``run_id``, keeping the LAST row.

    ``INDEX.jsonl`` is an append-only journal -- retried runs produce multiple
    entries with the same ``run_id``. Only one physical run directory exists
    per run_id, and its on-disk files reflect the most recent attempt.
    Keeping the LAST row maintains that correspondence.

    Returns: ``{run_id: index_row_dict}``.
    """
    if not INDEX_PATH.exists():
        raise FileNotFoundError(f"INDEX.jsonl not found at {INDEX_PATH}")
    latest: dict[str, dict] = {}
    for line in INDEX_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = row.get("run_id")
        if rid:
            latest[rid] = row
    return latest


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    """One B1/B2 pair for a single CVE.

    The pair is formed by ``cve_id`` (replicate fixed to 0 in v3). ``b1`` and
    ``b2`` each bundle the INDEX row, the mechanical.json, the meta.json, the
    audit.json, and the event-derived ``RunMetrics``.
    """

    cve_id: str
    b1_index: dict
    b2_index: dict
    b1_mech: dict
    b2_mech: dict
    b1_meta: dict
    b2_meta: dict
    b1_audit: dict
    b2_audit: dict
    b1_metrics: RunMetrics
    b2_metrics: RunMetrics
    b1_run_dir: Path
    b2_run_dir: Path


def build_pairs(index_by_id: dict[str, dict]) -> list[Pair]:
    """Pair B1 and B2 rows by ``cve_id``; skip CVEs missing either cell.

    Returns the pairs in a stable, CVE-alphabetical order so the per-CVE CSV
    rows are reproducible run-to-run.
    """
    rows_by_cve: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in index_by_id.values():
        rows_by_cve[row["cve_id"]][row["cell"]] = row

    pairs: list[Pair] = []
    for cve_id in sorted(rows_by_cve):
        cells = rows_by_cve[cve_id]
        b1 = cells.get(CELL_X)
        b2 = cells.get(CELL_Y)
        if b1 is None or b2 is None:
            logger.warning(
                "Skipping %s -- missing %s cell",
                cve_id,
                "B1" if b1 is None else "B2",
            )
            continue

        # Resolve the on-disk directories from the INDEX row; fall back to the
        # canonical layout if ``path`` is missing.
        b1_dir = Path(b1.get("path") or RUNS_ROOT / cve_id / CELL_X / "0")
        b2_dir = Path(b2.get("path") or RUNS_ROOT / cve_id / CELL_Y / "0")

        pairs.append(
            Pair(
                cve_id=cve_id,
                b1_index=b1,
                b2_index=b2,
                b1_mech=_load_json(b1_dir / "mechanical.json"),
                b2_mech=_load_json(b2_dir / "mechanical.json"),
                b1_meta=_load_json(b1_dir / "meta.json"),
                b2_meta=_load_json(b2_dir / "meta.json"),
                b1_audit=_load_json(b1_dir / "audit.json"),
                b2_audit=_load_json(b2_dir / "audit.json"),
                b1_metrics=compute_run_metrics(b1_dir, b1["run_id"]),
                b2_metrics=compute_run_metrics(b2_dir, b2["run_id"]),
                b1_run_dir=b1_dir,
                b2_run_dir=b2_dir,
            )
        )
    return pairs


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def mcnemar_paired(pairs: list[tuple[int, int]]) -> dict[str, Any]:
    """Exact McNemar test for paired binary outcomes.

    ``pairs`` is a list of (x, y) with x = B1 outcome (0/1), y = B2 outcome.
    We report:

    * ``b_y_wins``: pairs where x=0, y=1 (B2 improves on B1).
    * ``c_x_wins``: pairs where x=1, y=0 (B2 regresses from B1).
    * ``p_exact``: two-sided exact binomial test on the discordant count
      (scipy ``binomtest``; only discordants inform McNemar).
    * ``diff_rate``: mean(y) - mean(x) -- the direction of effect.
    * ``ci_lo``, ``ci_hi``: 95% bootstrap CI for ``diff_rate`` (paired
      resample of the CVE-level outcomes). Seed fixed to 42 for
      reproducibility, same as ``compute_metrics.py``.

    Degenerate cases:
      - 0 discordants -> p = 1.0 (cannot reject, consistent with binomtest).
      - 0 pairs -> all zeros.
    """
    from scipy.stats import binomtest

    n = len(pairs)
    b = sum(1 for x, y in pairs if x == 0 and y == 1)
    c = sum(1 for x, y in pairs if x == 1 and y == 0)
    disc = b + c
    p = binomtest(k=b, n=disc, p=0.5, alternative="two-sided").pvalue if disc else 1.0
    diff = (sum(y for _, y in pairs) - sum(x for x, _ in pairs)) / max(n, 1)

    rng = np.random.default_rng(42)
    if n == 0:
        lo = hi = 0.0
    else:
        arr = np.array(pairs, dtype=float)
        boots = np.empty(10_000, dtype=float)
        for i in range(10_000):
            idx = rng.integers(0, n, size=n)
            s = arr[idx]
            boots[i] = s[:, 1].mean() - s[:, 0].mean()
        lo, hi = np.percentile(boots, [2.5, 97.5])

    return {
        "n_pairs": n,
        "b_y_wins": b,
        "c_x_wins": c,
        "n_discordant": disc,
        "p_exact": float(p),
        "diff_rate": float(diff),
        "ci_lo": float(lo),
        "ci_hi": float(hi),
    }


def wilcoxon_paired(xs: list[float], ys: list[float]) -> dict[str, Any]:
    """Two-sided Wilcoxon signed-rank on paired continuous measurements.

    Used for cost, wallclock, and token metrics -- each CVE yields one B1
    and one B2 value. Falls back to (n, median) descriptive-only when the
    paired diffs are all zero (``scipy.stats.wilcoxon`` raises in that case).
    """
    from scipy.stats import wilcoxon

    assert len(xs) == len(ys), "paired arrays must have equal length"
    if not xs:
        return {"n": 0}

    diffs = [y - x for x, y in zip(xs, ys, strict=True)]
    res: dict[str, Any] = {
        "n": len(xs),
        "median_x": float(np.median(xs)),
        "median_y": float(np.median(ys)),
        "median_diff": float(np.median(diffs)),
        "mean_diff": float(np.mean(diffs)),
    }
    if all(d == 0 for d in diffs):
        res.update({"statistic": None, "p_two_sided": 1.0, "note": "all diffs zero"})
        return res
    wx_res = wilcoxon(xs, ys, zero_method="wilcox", alternative="two-sided")
    # ``wilcoxon`` returns a named tuple (``WilcoxonResult``); pyright cannot
    # narrow the Union return type, so we cast via ``float(...)`` on the
    # individual attribute access. ``statistic`` and ``pvalue`` are numpy
    # floats at runtime.
    res.update(
        {
            "statistic": float(wx_res.statistic),  # type: ignore[arg-type]
            "p_two_sided": float(wx_res.pvalue),  # type: ignore[arg-type]
        }
    )
    return res


def jaccard(a: set[int], b: set[int]) -> float:
    """Token-set Jaccard similarity; defined as 0 when both sets empty."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Prompt-level Jaccard (sibling + hierarchy)
# ---------------------------------------------------------------------------

def _sibling_jaccard(rm: RunMetrics, worker_only: bool = True) -> list[float]:
    """Pairwise Jaccard(first_prompt) over agents sharing a parent.

    ``worker_only`` (default True) restricts pairing to agents with
    ``role=="WORKER"``. This preserves the v2 semantic (REPORT §9 measured
    sibling Jaccard among WORKER children of a MANAGER). In v3, MANAGER
    agents now emit their own ``prompt_sent`` events (empirically verified
    on e.g. ``gpac.cve-2022-1795/B2``), so an unfiltered pass would also
    pair MANAGER vs MANAGER and change what the metric measures. Use
    ``worker_only=False`` only for the supplemental "full-tree" signal.
    """
    groups: dict[str | None, list[str]] = defaultdict(list)
    for aid, pid in rm.agent_parent.items():
        if aid not in rm.first_prompt_token_set:
            continue
        if worker_only and rm.agent_role.get(aid, "").upper() != "WORKER":
            continue
        groups[pid].append(aid)
    out: list[float] = []
    for pid, kids in groups.items():
        if pid is None or len(kids) < 2:
            continue
        for a, b in combinations(kids, 2):
            out.append(
                jaccard(rm.first_prompt_token_set[a], rm.first_prompt_token_set[b])
            )
    return out


def _nearest_prompted_ancestor(aid: str, rm: RunMetrics) -> str | None:
    """Walk up ``agent_parent`` to the nearest ancestor with a first prompt.

    In v2, MANAGERs did NOT issue ``prompt_sent`` events, so the nearest
    prompted ancestor of a WORKER was always the BOSS (via MANAGER skip).
    In v3, MANAGERs DO issue prompts -- so the nearest prompted ancestor of
    a WORKER is now usually its direct MANAGER. That is a real semantic
    shift between v2 and v3 "hierarchy" Jaccard, and we document it rather
    than trying to replicate v2 by skipping non-BOSS ancestors.
    """
    cur = rm.agent_parent.get(aid)
    while cur is not None:
        if cur in rm.first_prompt_token_set:
            return cur
        cur = rm.agent_parent.get(cur)
    return None


def _hierarchy_jaccard(rm: RunMetrics, worker_only: bool = True) -> list[float]:
    """Jaccard(first_prompt of nearest prompted ancestor, first_prompt of descendant).

    ``worker_only`` (default True) restricts the descendant side to WORKER
    agents, matching REPORT §9's "ancestor-worker" pair type label.
    """
    out: list[float] = []
    for aid in rm.first_prompt_token_set:
        if worker_only and rm.agent_role.get(aid, "").upper() != "WORKER":
            continue
        anc = _nearest_prompted_ancestor(aid, rm)
        if anc is None:
            continue
        out.append(
            jaccard(rm.first_prompt_token_set[anc], rm.first_prompt_token_set[aid])
        )
    return out


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    """Write a list of row dicts to ``path``, creating parents on demand.

    Columns default to the sorted union of all keys so the schema is
    self-describing and stable for reproducibility. Missing keys per row are
    treated as blank.
    """
    if not rows:
        logger.warning("No rows for %s; writing empty file with no header.", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = columns or sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    logger.info("Wrote %s (%d rows, %d cols)", path, len(rows), len(cols))


def _index_num(row: dict, key: str, default: float = 0.0) -> float:
    """Fetch a numeric field from an INDEX row, coercing None/absent to default."""
    v = row.get(key)
    return float(v) if v is not None else default


def _mech_pass(mech_json: dict, index_row: dict, phase: str) -> bool:
    """Resolve the mechanical pass for a phase.

    Priority:
      1. On-disk ``mechanical.json`` (authoritative -- computed by the
         evaluator at finalise time).
      2. ``INDEX.mechanical_pass[phase]`` fallback (might be stale if the
         journal row is from an earlier attempt, even after dedup).

    ``phase`` is one of {builder, exploiter, fixer, end_to_end}.
    """
    if mech_json:
        key = {
            "builder": "builder_pass",
            "exploiter": "exploiter_pass",
            "fixer": "fixer_pass",
            "end_to_end": "end_to_end_pass",
        }[phase]
        v = mech_json.get(key)
        if isinstance(v, bool):
            return v
    mp = index_row.get("mechanical_pass") or {}
    return bool(mp.get(phase))


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------

def build_per_cve_rows(pairs: list[Pair]) -> list[dict]:
    """One row per CVE with B1/B2 values and B2 - B1 deltas for every metric.

    Column naming convention: ``<metric>_B1``, ``<metric>_B2``, ``<metric>_delta``.
    Deltas are numeric when both sides are numeric, otherwise empty.

    This is the table the report's per-instance sections should render
    directly from -- no further aggregation needed.
    """
    rows: list[dict] = []
    for pair in pairs:
        b1, b2 = pair.b1_metrics, pair.b2_metrics

        row: dict[str, Any] = {
            "cve_id": pair.cve_id,
            # --- Outcome (mechanical evaluator) ---
            "mech_end_to_end_B1": _mech_pass(pair.b1_mech, pair.b1_index, "end_to_end"),
            "mech_end_to_end_B2": _mech_pass(pair.b2_mech, pair.b2_index, "end_to_end"),
            "mech_builder_B1": _mech_pass(pair.b1_mech, pair.b1_index, "builder"),
            "mech_builder_B2": _mech_pass(pair.b2_mech, pair.b2_index, "builder"),
            "mech_exploiter_B1": _mech_pass(pair.b1_mech, pair.b1_index, "exploiter"),
            "mech_exploiter_B2": _mech_pass(pair.b2_mech, pair.b2_index, "exploiter"),
            "mech_fixer_B1": _mech_pass(pair.b1_mech, pair.b1_index, "fixer"),
            "mech_fixer_B2": _mech_pass(pair.b2_mech, pair.b2_index, "fixer"),
            # --- Deliverable production (on-disk scan, llvmsymbol filtered) ---
            "patch_on_disk_B1": has_patch(pair.b1_run_dir),
            "patch_on_disk_B2": has_patch(pair.b2_run_dir),
            "report_on_disk_B1": has_report(pair.b1_run_dir),
            "report_on_disk_B2": has_report(pair.b2_run_dir),
            "poc_on_disk_B1": has_poc(pair.b1_run_dir),
            "poc_on_disk_B2": has_poc(pair.b2_run_dir),
            # --- Cost / resources (from INDEX, cross-checked against event totals) ---
            "total_cost_usd_B1": _index_num(pair.b1_index, "total_cost_usd"),
            "total_cost_usd_B2": _index_num(pair.b2_index, "total_cost_usd"),
            "wallclock_seconds_B1": _index_num(pair.b1_index, "wallclock_seconds"),
            "wallclock_seconds_B2": _index_num(pair.b2_index, "wallclock_seconds"),
            "event_count_B1": _index_num(pair.b1_index, "event_count"),
            "event_count_B2": _index_num(pair.b2_index, "event_count"),
            "termination_reason_B1": pair.b1_index.get("termination_reason"),
            "termination_reason_B2": pair.b2_index.get("termination_reason"),
            # --- Token buckets ---
            # ``*_total`` = tree-side ``tokens_consumed`` PLUS Claude Agent
            # SDK's ``worker_cost_recorded``. These are the numbers to quote
            # against REPORT §5 (7-12 M cache_read per run). Separately we
            # expose the ``*_tc`` and ``*_wcr`` splits so the breakdown is
            # auditable -- e.g. "BOSS input tokens" lives in ``_tc`` only,
            # "worker cache_read" lives in ``_wcr`` only.
            "input_tokens_total_B1": b1.tc_input_tokens + b1.wcr_input_tokens,
            "input_tokens_total_B2": b2.tc_input_tokens + b2.wcr_input_tokens,
            "output_tokens_total_B1": b1.tc_output_tokens + b1.wcr_output_tokens,
            "output_tokens_total_B2": b2.tc_output_tokens + b2.wcr_output_tokens,
            "cache_read_tokens_total_B1": b1.tc_cache_read_tokens + b1.wcr_cache_read_tokens,
            "cache_read_tokens_total_B2": b2.tc_cache_read_tokens + b2.wcr_cache_read_tokens,
            "cache_creation_tokens_total_B1": (
                b1.tc_cache_creation_tokens + b1.wcr_cache_creation_tokens
            ),
            "cache_creation_tokens_total_B2": (
                b2.tc_cache_creation_tokens + b2.wcr_cache_creation_tokens
            ),
            # Tree-side (BOSS / MANAGER / condense) split.
            "tc_input_tokens_B1": b1.tc_input_tokens,
            "tc_input_tokens_B2": b2.tc_input_tokens,
            "tc_output_tokens_B1": b1.tc_output_tokens,
            "tc_output_tokens_B2": b2.tc_output_tokens,
            "tc_cache_read_tokens_B1": b1.tc_cache_read_tokens,
            "tc_cache_read_tokens_B2": b2.tc_cache_read_tokens,
            "tc_cache_creation_tokens_B1": b1.tc_cache_creation_tokens,
            "tc_cache_creation_tokens_B2": b2.tc_cache_creation_tokens,
            "tc_cost_usd_B1": b1.tc_cost_usd,
            "tc_cost_usd_B2": b2.tc_cost_usd,
            # Worker SDK split (the Sonnet workers' own cost envelope).
            "wcr_input_tokens_B1": b1.wcr_input_tokens,
            "wcr_input_tokens_B2": b2.wcr_input_tokens,
            "wcr_output_tokens_B1": b1.wcr_output_tokens,
            "wcr_output_tokens_B2": b2.wcr_output_tokens,
            "wcr_cache_read_tokens_B1": b1.wcr_cache_read_tokens,
            "wcr_cache_read_tokens_B2": b2.wcr_cache_read_tokens,
            "wcr_cache_creation_tokens_B1": b1.wcr_cache_creation_tokens,
            "wcr_cache_creation_tokens_B2": b2.wcr_cache_creation_tokens,
            "wcr_cost_usd_B1": b1.wcr_cost_usd,
            "wcr_cost_usd_B2": b2.wcr_cost_usd,
            # --- Tool activity (NEW in v3) ---
            "tool_calls_total_B1": b1.tool_calls_total,
            "tool_calls_total_B2": b2.tool_calls_total,
            "security_tool_adoption_B1": b1.security_tool_adoption,
            "security_tool_adoption_B2": b2.security_tool_adoption,
            "security_tool_invocations_B1": b1.security_tool_invocations,
            "security_tool_invocations_B2": b2.security_tool_invocations,
            # --- Redundancy (NEW in v3) ---
            "redundancy_intra_B1": b1.redundancy_intra,
            "redundancy_intra_B2": b2.redundancy_intra,
            "redundancy_sibling_B1": b1.redundancy_sibling,
            "redundancy_sibling_B2": b2.redundancy_sibling,
            "redundancy_hierarchy_B1": b1.redundancy_hierarchy,
            "redundancy_hierarchy_B2": b2.redundancy_hierarchy,
            "redundancy_rate_total_B1": b1.redundancy_rate_total,
            "redundancy_rate_total_B2": b2.redundancy_rate_total,
            # --- Audit violations (NEW in v3; count only, types in separate CSV) ---
            "audit_violations_B1": int(pair.b1_index.get("audit_violations") or 0),
            "audit_violations_B2": int(pair.b2_index.get("audit_violations") or 0),
            # --- CNR ---
            "cnr_mean_B1": b1.cnr_mean,
            "cnr_mean_B2": b2.cnr_mean,
            "cnr_n_prompts_B1": b1.cnr_n_prompts,
            "cnr_n_prompts_B2": b2.cnr_n_prompts,
        }

        # Derive <metric>_delta for every _B1/_B2 pair where both are numeric.
        # We compute delta = B2 - B1 (treatment minus baseline). Booleans are
        # coerced to int -- the natural reading is "+1 = B2 achieved it, B1 did not".
        for col in list(row):
            if col.endswith("_B1"):
                base = col[:-3]
                v1 = row[col]
                v2 = row.get(f"{base}_B2")
                if isinstance(v1, bool) or isinstance(v2, bool):
                    row[f"{base}_delta"] = int(bool(v2)) - int(bool(v1))
                elif isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                    row[f"{base}_delta"] = v2 - v1
        rows.append(row)
    return rows


def build_tool_breakdown_rows(pairs: list[Pair]) -> list[dict]:
    """Per-tool call counts summed across all runs in each cell, plus shares.

    Answer: does the briefing shift the tool mix? E.g. REPORT §7 showed A1
    delegated to ``Agent`` but A2 (no subagents) shifted to ``Bash``. Here we
    do the equivalent B1 vs B2 breakdown, now measurable for the first time.
    """
    # Aggregate Counter per cell across all runs.
    totals: dict[str, Counter[str]] = {CELL_X: Counter(), CELL_Y: Counter()}
    for pair in pairs:
        totals[CELL_X].update(pair.b1_metrics.tool_calls_by_name)
        totals[CELL_Y].update(pair.b2_metrics.tool_calls_by_name)

    all_tools = sorted(set(totals[CELL_X]) | set(totals[CELL_Y]))
    grand: dict[str, int] = {
        CELL_X: sum(totals[CELL_X].values()),
        CELL_Y: sum(totals[CELL_Y].values()),
    }

    rows: list[dict] = []
    for tool in all_tools:
        n1 = totals[CELL_X].get(tool, 0)
        n2 = totals[CELL_Y].get(tool, 0)
        rows.append(
            {
                "tool_name": tool,
                f"count_{CELL_X}": n1,
                f"count_{CELL_Y}": n2,
                f"share_{CELL_X}": (n1 / grand[CELL_X]) if grand[CELL_X] else 0.0,
                f"share_{CELL_Y}": (n2 / grand[CELL_Y]) if grand[CELL_Y] else 0.0,
                "count_delta": n2 - n1,
            }
        )
    # Append a TOTAL row for easy sanity reading.
    rows.append(
        {
            "tool_name": "__TOTAL__",
            f"count_{CELL_X}": grand[CELL_X],
            f"count_{CELL_Y}": grand[CELL_Y],
            f"share_{CELL_X}": 1.0 if grand[CELL_X] else 0.0,
            f"share_{CELL_Y}": 1.0 if grand[CELL_Y] else 0.0,
            "count_delta": grand[CELL_Y] - grand[CELL_X],
        }
    )
    return rows


def build_audit_breakdown_rows(pairs: list[Pair]) -> list[dict]:
    """Per-violation-type counts per cell.

    Reads the ``violations`` list from each audit.json. Reporting types (not
    just totals) is necessary because the briefing's anti-cheat clause
    addresses specific classes (git_log_all, webfetch_external) and the
    B1-vs-B2 signal is type-conditional.
    """
    by_cell: dict[str, Counter[str]] = {CELL_X: Counter(), CELL_Y: Counter()}
    for pair in pairs:
        for c, audit in ((CELL_X, pair.b1_audit), (CELL_Y, pair.b2_audit)):
            for v in audit.get("violations") or []:
                vt = v.get("type") or "unknown"
                by_cell[c][vt] += 1

    all_types = sorted(set(by_cell[CELL_X]) | set(by_cell[CELL_Y]))
    rows = [
        {
            "violation_type": vt,
            f"count_{CELL_X}": by_cell[CELL_X].get(vt, 0),
            f"count_{CELL_Y}": by_cell[CELL_Y].get(vt, 0),
            "count_delta": by_cell[CELL_Y].get(vt, 0) - by_cell[CELL_X].get(vt, 0),
        }
        for vt in all_types
    ]
    rows.append(
        {
            "violation_type": "__TOTAL__",
            f"count_{CELL_X}": sum(by_cell[CELL_X].values()),
            f"count_{CELL_Y}": sum(by_cell[CELL_Y].values()),
            "count_delta": sum(by_cell[CELL_Y].values()) - sum(by_cell[CELL_X].values()),
        }
    )
    return rows


def build_termination_rows(pairs: list[Pair]) -> list[dict]:
    """Cross-tab of termination_reason x cell, including share within cell."""
    by_cell: dict[str, Counter[str]] = {CELL_X: Counter(), CELL_Y: Counter()}
    for pair in pairs:
        by_cell[CELL_X][pair.b1_index.get("termination_reason") or "unknown"] += 1
        by_cell[CELL_Y][pair.b2_index.get("termination_reason") or "unknown"] += 1
    all_terms = sorted(set(by_cell[CELL_X]) | set(by_cell[CELL_Y]))
    total_x = sum(by_cell[CELL_X].values())
    total_y = sum(by_cell[CELL_Y].values())
    return [
        {
            "termination_reason": t,
            f"count_{CELL_X}": by_cell[CELL_X].get(t, 0),
            f"count_{CELL_Y}": by_cell[CELL_Y].get(t, 0),
            f"share_{CELL_X}": (by_cell[CELL_X].get(t, 0) / total_x) if total_x else 0.0,
            f"share_{CELL_Y}": (by_cell[CELL_Y].get(t, 0) / total_y) if total_y else 0.0,
        }
        for t in all_terms
    ]


def build_prompt_redundancy_rows(pairs: list[Pair]) -> list[dict]:
    """Sibling + hierarchy Jaccard per run, plus per-cell summary rows.

    For each cell we emit TWO scope tags:

    * ``scope="worker_only"`` -- pairs restricted to ``role="WORKER"``
      agents. This is the v2-comparable measurement (REPORT §9 labelled
      the hierarchy pair type "ancestor-worker", and sibling Jaccard was
      computed over WORKER children of a single MANAGER).
    * ``scope="full_tree"`` -- unfiltered pairs, i.e. including the new v3
      MANAGER vs MANAGER / BOSS vs MANAGER pairings that only exist because
      the v3 adapter now records MANAGER ``prompt_sent`` events.

    Reporting both lets the report say "v2-equivalent sibling Jaccard moved
    from 0.946 (B2) to X.XXX in v3" without silently changing the
    definition of the metric.
    """
    rows: list[dict] = []
    # cell -> scope -> pair_type -> values.
    per_cell: dict[str, dict[str, dict[str, list[float]]]] = {
        CELL_X: {
            "worker_only": {"sibling": [], "hierarchy": []},
            "full_tree": {"sibling": [], "hierarchy": []},
        },
        CELL_Y: {
            "worker_only": {"sibling": [], "hierarchy": []},
            "full_tree": {"sibling": [], "hierarchy": []},
        },
    }

    for pair in pairs:
        for cell, rm in ((CELL_X, pair.b1_metrics), (CELL_Y, pair.b2_metrics)):
            for scope, worker_only in (("worker_only", True), ("full_tree", False)):
                for j in _sibling_jaccard(rm, worker_only=worker_only):
                    rows.append(
                        {
                            "kind": "pair",
                            "cve_id": pair.cve_id,
                            "cell": cell,
                            "scope": scope,
                            "pair_type": "sibling",
                            "jaccard": j,
                        }
                    )
                    per_cell[cell][scope]["sibling"].append(j)
                for j in _hierarchy_jaccard(rm, worker_only=worker_only):
                    rows.append(
                        {
                            "kind": "pair",
                            "cve_id": pair.cve_id,
                            "cell": cell,
                            "scope": scope,
                            "pair_type": "hierarchy",
                            "jaccard": j,
                        }
                    )
                    per_cell[cell][scope]["hierarchy"].append(j)

    # One summary row per (cell, scope, pair_type) so consumers do not have
    # to re-aggregate. When n==0 we still emit the row with nulls so the
    # CSV schema stays stable.
    for cell in (CELL_X, CELL_Y):
        for scope in ("worker_only", "full_tree"):
            for pt in ("sibling", "hierarchy"):
                values = per_cell[cell][scope][pt]
                summary_row: dict[str, Any] = {
                    "kind": "summary",
                    "cve_id": "__ALL__",
                    "cell": cell,
                    "scope": scope,
                    "pair_type": pt,
                    "jaccard": None,
                    "n": len(values),
                }
                if values:
                    summary_row.update(
                        {
                            "mean": statistics.fmean(values),
                            "median": statistics.median(values),
                            "min": min(values),
                            "max": max(values),
                        }
                    )
                else:
                    summary_row.update(
                        {"mean": None, "median": None, "min": None, "max": None}
                    )
                rows.append(summary_row)
    return rows


def build_summary_rows(
    pairs: list[Pair], per_cve_rows: list[dict]
) -> list[dict]:
    """Aggregate + paired-stat summary.

    Structure: one row per metric with:
      - per-cell mean, median, min, max
      - McNemar stats for binary metrics
      - Wilcoxon stats for continuous metrics
      - ``inclusion_policy`` = ``all`` or ``completed_only`` (we emit both).

    Rationale for two inclusion policies
    ------------------------------------
    - ``all`` (primary): uses all 10 pairs, no filtering. This is the honest
      comparison because B2's tendency to time-out IS the effect of the
      briefing; filtering it out would bias toward "conditional on completion".
    - ``completed_only`` (sensitivity): drops pairs where either side has a
      non-``completed`` termination. Reports "does the briefing help *when the
      run completes*?". In v3, ``completed_only`` is a small subset (B1 7/10
      completed, B2 1/10), so this policy is mostly descriptive; we still emit
      it for transparency.
    """
    out: list[dict] = []

    # Gather paired vectors per metric from the per_cve_rows. We collect these
    # from per_cve_rows (rather than re-deriving) to guarantee the summary
    # sees exactly the values that were written out.
    def vec(metric_name: str) -> tuple[list[Any], list[Any]]:
        xs, ys = [], []
        for r in per_cve_rows:
            xs.append(r.get(f"{metric_name}_B1"))
            ys.append(r.get(f"{metric_name}_B2"))
        return xs, ys

    binary_metrics = [
        "mech_end_to_end",
        "mech_builder",
        "mech_exploiter",
        "mech_fixer",
        "patch_on_disk",
        "report_on_disk",
        "poc_on_disk",
        "security_tool_adoption",
    ]
    continuous_metrics = [
        "total_cost_usd",
        "wallclock_seconds",
        "event_count",
        # Combined token totals (tokens_consumed + worker_cost_recorded).
        "input_tokens_total",
        "output_tokens_total",
        "cache_read_tokens_total",
        "cache_creation_tokens_total",
        # Per-event-family splits for audit / mechanism diagnosis.
        "tc_cache_read_tokens",
        "tc_cost_usd",
        "wcr_cache_read_tokens",
        "wcr_cost_usd",
        "tool_calls_total",
        "security_tool_invocations",
        "redundancy_intra",
        "redundancy_sibling",
        "redundancy_hierarchy",
        "redundancy_rate_total",
        "audit_violations",
        "cnr_mean",
    ]

    # Completed-only mask: both cells have termination_reason == "completed".
    completed_mask = [
        (r.get("termination_reason_B1") == "completed")
        and (r.get("termination_reason_B2") == "completed")
        for r in per_cve_rows
    ]

    def _agg(values: list[Any]) -> dict[str, Any]:
        nums = [v for v in values if isinstance(v, (int, float, bool)) and v is not None]
        nums_f = [float(v) for v in nums]
        if not nums_f:
            return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
        return {
            "n": len(nums_f),
            "mean": statistics.fmean(nums_f),
            "median": statistics.median(nums_f),
            "min": min(nums_f),
            "max": max(nums_f),
        }

    def _emit(metric: str, policy: str, xs: list[Any], ys: list[Any]) -> dict:
        # Drop pairs where either side is None (missing / not applicable).
        paired = [
            (x, y)
            for x, y in zip(xs, ys, strict=True)
            if x is not None and y is not None
        ]
        row = {
            "metric": metric,
            "inclusion_policy": policy,
            "n_pairs": len(paired),
            **{f"{k}_{CELL_X}": v for k, v in _agg(xs).items()},
            **{f"{k}_{CELL_Y}": v for k, v in _agg(ys).items()},
        }
        if metric in binary_metrics:
            pairs_int = [(int(bool(x)), int(bool(y))) for x, y in paired]
            mc = mcnemar_paired(pairs_int)
            row["test"] = "McNemar exact binomial"
            row.update({k: v for k, v in mc.items() if k not in {"n_pairs"}})
        elif metric in continuous_metrics:
            try:
                wx = wilcoxon_paired(
                    [float(x) for x, _ in paired],
                    [float(y) for _, y in paired],
                )
                row["test"] = "Wilcoxon signed-rank (two-sided)"
                row.update({k: v for k, v in wx.items() if k != "n"})
            except Exception as exc:  # noqa: BLE001
                # If scipy raises for any reason (e.g. library import missing
                # in the execution environment) we still emit descriptive
                # aggregates so the row is not lost.
                row["test"] = f"Wilcoxon signed-rank (failed: {exc!r})"
        return row

    for metric in binary_metrics + continuous_metrics:
        xs, ys = vec(metric)
        out.append(_emit(metric, "all", xs, ys))
        xs_c = [x for x, keep in zip(xs, completed_mask, strict=True) if keep]
        ys_c = [y for y, keep in zip(ys, completed_mask, strict=True) if keep]
        out.append(_emit(metric, "completed_only", xs_c, ys_c))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Build all v3 B1-vs-B2 comparison tables.

    Not executed automatically -- invoke explicitly with ``python -m`` or
    ``uv run python ...``.
    """
    index_by_id = load_index()
    logger.info("Loaded %d unique runs from %s", len(index_by_id), INDEX_PATH)

    pairs = build_pairs(index_by_id)
    logger.info("Built %d B1/B2 pairs", len(pairs))

    per_cve_rows = build_per_cve_rows(pairs)
    _write_csv(OUT_DIR / "per_cve.csv", per_cve_rows)

    summary_rows = build_summary_rows(pairs, per_cve_rows)
    _write_csv(OUT_DIR / "summary.csv", summary_rows)

    _write_csv(OUT_DIR / "termination.csv", build_termination_rows(pairs))
    _write_csv(OUT_DIR / "tool_breakdown.csv", build_tool_breakdown_rows(pairs))
    _write_csv(OUT_DIR / "audit_breakdown.csv", build_audit_breakdown_rows(pairs))
    _write_csv(OUT_DIR / "prompt_redundancy.csv", build_prompt_redundancy_rows(pairs))

    logger.info("All tables written to %s", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
