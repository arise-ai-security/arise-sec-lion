# Context-Handoff Gaps Between Hierarchical Phases

> Status: DRAFT v0.1 (2026-05-15). Discovered while running the B1 continuous-batch experiment (`temp/b1-continuous-batch-prompt.md`) and analysing 32 completed b1-batch-autogen runs.
> Owner: nearKim. Mitigation prompts already landed; see "Mitigations applied".

## Problem statement

In the hierarchical pipeline (B1, B2, C1, C2) the Builder phase compiles an ASan-instrumented binary that every downstream phase (Exploiter, Fixer, Reporter) is expected to invoke. The handoff mechanism is the cross-phase shared-context decisions (`DecisionRecorded` events with a `decision_key` like `binary_path`, `binary_location`, etc.) plus the per-worker `<context-update>` blocks.

**The mechanism is unreliable.** In the b1-batch-autogen study (32 b1-batch-autogen runs, mix of `exiv2`, `faad2`, `gpac`, `njs`), the Builder phase completed successfully in 28/32 runs but **explicitly published a usable binary path in only 11/32 (34%)** of those Build-Compiler workers. The remaining 18 Build-Compilers published only generic `build_status: "successful"` / `asan_build_complete: "..."`-style decisions, and 3 Build-Compilers published no decisions at all.

Downstream phases compensate by re-discovering the binary themselves via shell tools. That re-discovery is a measurable budget leak and, on harder CVEs where the per-step watchdog is the binding constraint, the wasted iterations correlate with watchdog-timeout failures.

## Evidence

### Builder side — what gets handed off

```
Build-Compiler workers (n=32)            Build-Verifier workers (n=32)
  published binary_path / location: 11    published binary_path / location: 1-2
  published only generic build_status: 18  published only generic verification: 28+
  published no decisions at all:      3    (Build-Verifier currently does not re-publish
                                            the binary path produced by Build-Compiler.)
```

Sample handoff values that **do** work (downstream can use them directly):
- `"/work/bin/MP4Box (7.9M, ASan-instrumented, tested and verified working)"`
- `"/work/bin/faad (3.1 MB, ASan-instrumented)"`
- `"Main binaries: /src/gpac/bin/gcc/MP4Box (54M), /src/gpac/bin/gcc/gpac (52M)"`

Sample handoff values that **do not** (no path, downstream still has to search):
- `"ASan build completed successfully"`
- `"Build completed successfully with exit code 0"`
- `"faad2 compiled successfully with AddressSanitizer flags ..."`

### Downstream side — discovery overhead per worker role

Across all 32 b1-batch-autogen runs, downstream workers that touched a binary:

| Role | Workers | `ls /work/bin*` | `which`/`whereis` | `find -name` |
|---|---|---|---|---|
| PoC-Tester | 27 | 16 (59%) | 21 (78%) | 5 (19%) |
| Repro-Creator | 27 | 10 (37%) | 21 (78%) | 2 |
| Exploit-Validator | 27 | 13 (48%) | 20 (74%) | 0 |
| Forward-Instrumentator | 27 | 3 | 21 | 2 |
| Patch-Validator | 21 | 7 (33%) | 13 (62%) | 3 |

Concrete worker example — PoC-Tester `f8a1eea3` (CVE-2018-20358): 4 separate `ls /work/bin/...` discovery calls out of 18 total tool calls; ≈22% of its tool budget burned just confirming where the binary lives.

### Correlation with failed runs

Per-run correlation (n=32 b1-batch-autogen runs, exit_status from `runs/<id>/run_manifest.json`):

| Builder handoff | n | run_success | run_failed | avg failed exploiter workers per run |
|---|---|---|---|---|
| Published binary path (true) | 9 | 8 | 1 | 0.33 |
| Did NOT publish path (false) | 17 | 14 | 3 | 0.47 |
| No Build-Compiler decisions | 6 | 5 | 1 | 0.50 |

The two clearly failed runs with the highest exploiter-worker failure count — `ae19708e` (faad2-2018-20194) and `ae1caf42` (gpac-2022-2454), each with 3 timed-out exploiter workers — both had **no published binary path**. **However**, walking those workers via `scripts/show_worker_events.py` shows they died from a different proximate cause: the watchdog fired at exactly 900s with zero LLM tokens consumed. They never acquired the LLM semaphore at all. That is the queue-race condition that commit `3f85024 Fix watchdog clock to start at LLM semaphore acquire` was meant to address; with `orchestration.concurrency.max_concurrent_llm_calls` recently bumped to 8 and `--parallel` growing, the floor is being hit again.

**Therefore the handoff-gap is a real budget leak but it is not the proximate cause of the failed runs in the current dataset.** It will become a proximate cause as the difficulty of the remaining 168 CVEs increases — already in the successful runs we see PoC-Testers spending a fifth of their tool budget on rediscovery.

## Root cause

The Builder phase's `<context-update>` instruction in `prompts/domains/secbench/manager/builder.j2` was non-specific:

> `context_sharing`: Publish build status (success/failure) and binary locations via `<context-update>`.

"Binary locations" is a request, not a contract. There was no structured key, no example value, and no success criterion enforcing the handoff. The Build-Compiler worker has plenty of plausible alternatives ("ASan build completed successfully" is technically a binary location communication — the binaries are wherever the build script put them) and the success-rate of the phase rewards verbosity over discipline.

Downstream prompts (`exploiter.j2`, `fixer.j2`) likewise made no specific instruction to *read peer decisions before running discovery commands*. Each downstream worker is given the path `/work/bin/` as a hint and a list of tools — the path of least resistance is `ls /work/bin/`, especially because the per-worker prompt does not even reference shared-context decisions.

## Mitigations applied (2026-05-15)

1. `prompts/domains/secbench/manager/builder.j2`
   - **Build-Compiler**: now has a `⚠️ MANDATORY` block requiring it to (a) identify the primary ASan-instrumented binary, (b) ensure it lives at `/work/bin/<name>` (copy/symlink if not), (c) publish a `binary_path` decision with the absolute path, and (d) NOT close the task with only a generic "build successful" decision. The `context_sharing` section now lists `binary_path` as REQUIRED.
   - **Build-Verifier**: now must read peer decisions, verify `binary_path` was published, run `ls -lh` and `nm | grep asan` on it, and re-publish a `verified_binary_path` decision. If Build-Compiler skipped the handoff, Build-Verifier re-discovers and publishes itself.

2. `prompts/domains/secbench/manager/exploiter.j2`
   - New `⚠️ MANDATORY: Read binary_path Before Running Discovery Commands` section that every subtask's `security_insights` MUST include verbatim. The directive forbids discovery commands until peer decisions have been checked.

3. `prompts/domains/secbench/manager/fixer.j2`
   - Same MANDATORY section, additionally instructing workers to also inspect `/testcase/repro.sh` (which carries the Exploiter-validated invocation) before any discovery command.

These edits do not change the orchestrator, the event store, or any port — they tighten the prompts in `plugins/security`'s prompt tree and so are cybersecurity-scoped per the project's layer rules.

## Open questions / follow-ups

- **Will the mitigations actually take?** The Build-Compiler prompt is now explicit, but no enforcement guard exists in code — a worker that ignores the prompt will still complete the phase. Add an audit query after the next batch run: `SELECT count(*) FILTER (WHERE binary_path published) / count(*)` for Build-Compilers in `b1-batch-autogen` runs created after 2026-05-15.
- **Should the prompt-level fix be backed by an orchestrator-level guarantee?** Two options:
  - (A) A post-Builder hook in the Builder phase that scans `decisions` for the `binary_path` key and short-circuits the phase as `failed` if absent. Lives in `core/` or `plugins/security/` — but `plugins/` is meant for cybersecurity logic, not orchestration, so the appropriate home is a domain-specific phase-completion check in `plugins/security/` that returns a feasibility verdict the orchestrator already consumes.
  - (B) Drop a real value object — `BuilderHandoff(binary_path, additional_paths, asan_verified)` — into the shared context, and make the Exploiter-phase planner block on its presence. More invasive; cleanly typed.
- **Distinguish budget leak vs. watchdog race.** The two are now confounded. A clean way to attribute the next batch's failures is to extend the `show_worker_events.py` post-mortem to flag (a) `llm_tokens == 0 AND wall_time ≈ 900s` (watchdog before semaphore — known commit-3f85024 territory) versus (b) `llm_tokens > 0 AND last_thought_contains('/work/bin')` (handoff-gap budget leak). Both are pre-existing problems; they are not the same problem.
- **Generalisation beyond `/work/bin`.** The same silent-handoff pattern almost certainly applies to other implicit deliverables — `/testcase/base_commit_hash` (verified explicitly already), `/testcase/repro.sh` invocation form, sanitizer-error type from the bug report, target file paths. A future audit should enumerate decisions Build-Verifier publishes vs. what Exploiter workers re-derive.
- **Add to threats-to-validity (`03-threats-to-validity.md`, when it exists)** as TV-CH1: implicit shared-context contracts. A B1 vs A1 comparison is supposed to isolate the *coordination structure* under an identical worker. If the coordination structure leaks budget on implicit handoffs, the comparison is contaminated by the leak rather than measuring the structure itself.

## Reproducer / audit queries

The SQL used to produce the numbers above (also runnable as one-shot via `docker exec -i arise-db psql -U arise -d arise_events`):

```sql
-- Builder-side: how many Build-Compilers published a usable binary path?
WITH bc AS (
  SELECT aggregate_id AS worker_id
  FROM events
  WHERE event_type = 'TaskAssigned'
    AND payload->>'task_description' ~ '^\[Build-Compiler\]'
), per_worker AS (
  SELECT bc.worker_id,
    bool_or(
      e.event_type = 'DecisionRecorded'
      AND (
        (e.payload->>'decision_key' ~* 'binary.*(path|location)'
         AND e.payload->>'decision_value' ~ '/(work/bin|src|bin)/')
        OR e.payload->>'decision_value' ~ '/work/bin/[^ ]+'
      )
    ) AS published_binary_path
  FROM bc
  LEFT JOIN events e
         ON (e.payload->>'decided_by')::uuid = bc.worker_id
  GROUP BY bc.worker_id
)
SELECT published_binary_path, count(*) FROM per_worker GROUP BY 1;

-- Downstream side: how many exploiter/fixer workers ran ls /work/bin or which/whereis?
-- (Recursive walk of the run hierarchy; see code in temp/ for the helper that
-- materialises the root-id list from runs/*/run_manifest.json.)
```

The full per-run script and root-id materialisation live in this PR's working notes; promoting them into `experiments/shared/scripts/` as a reusable audit script is a follow-up.
