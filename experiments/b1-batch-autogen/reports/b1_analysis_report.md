---
generated_at: '2026-05-18T16:36:33.194422Z'
generated_by: experiments/shared/scripts/b1_analysis.py
inputs:
- experiments/b1-batch-autogen/reports/b1_analysis_metrics.csv
- experiments/b1-batch-autogen/reports/b1_analysis_hierarchy.csv
- experiments/b1-batch-autogen/reports/b1_analysis_summary.json
inputs_sha256:
  experiments/b1-batch-autogen/reports/b1_analysis_hierarchy.csv: a32ac62630f53eeba8a14e37c313660fd250c71f83551b1c69a2d70c6fddba55
  experiments/b1-batch-autogen/reports/b1_analysis_metrics.csv: c6bcae4a0a60218681ba444549be317f4f174f706b3b8dfc2429fd77c0738f64
  experiments/b1-batch-autogen/reports/b1_analysis_summary.json: fdd0c7d0187dcf8062b9f8782d2a380e059254cef4a40597ff409022f92d9417
  experiments/shared/templates/b1-analysis-report.md.j2: 8a7732b195208dfcf3aa692741f6d18adcf96eb063196f2d14dd405d75cbdf9d
output_sha256: dd633bb3bb1321b5ababebcf6d8ac46c6e38857ef98ef84df89eca34ea337fbf
template: experiments/shared/templates/b1-analysis-report.md.j2
---

# B1 Recursive-Orchestrator Analysis Report

Generated at: `2026-05-18T16:36:33.166209Z`

Study: `b1-batch-autogen`

## Bottom Line

| Metric | B1 |
|---|---:|
| Enrolled runs | 123 |
| Terminal runs | 123 |
| Successful terminal runs | 81 |
| Terminal success rate | 65.9% |
| Full real pipeline success | 0 |
| Total observed cost | $796.04 |

## Cohort Accounting

Membership is reconstructed from host-local sources only — there is no Postgres `events`
table for B1 in this analysis. The source scope is the intersection of:

1. **CSV events** from `/Users/garfield/b1_export/b1_events.csv.gz` (decoded to
   `EventRow` rows, grouped by run root via transitive `AgentCreated.parent_id` walk
   — `b1_analysis.load_b1_events_by_run`).
2. **Local run files** under `~/b1_export/runs/<run_id>/` (each must contain
   `run_manifest.json` and `events.jsonl` — `b1_analysis.discover_b1_run_files`).

| Check | Value |
|---|---:|
| Source scope | csv_events_intersect_local_runs |
| Dataset CVE tasks | 122 |
| CSV total event rows | 131023 |
| CSV run roots (RunStarted) | 123 |
| Runs with `run_manifest.json` | 123 |
| Runs with `events.jsonl` | 123 |
| Enrolled runs (intersection) | 123 |
| Terminal rows | 123 |
| Nonterminal rows | 0 |
| Missing-cost rows | 7 |
| Environmental failures | 13 |
| CSV/runs event-id mismatches | 0 |
| CSV/runs full-event mismatches | 0 |
| CSV/runs cost mismatches | 0 |

For each enrolled run, the analysis fetches the subtree from the CSV (boss aggregate plus
all transitively-spawned descendants) and compares it event-for-event with the local
`events.jsonl` (`b1_analysis.build_b1_run_row` calling `_events_records_match` +
`_events_costs_match`). The report is emitted only when ordered event ids, full event
records, and recomputed total cost agree. Disagreement aborts the whole report —
apple-to-apple with the a12 DB-vs-runs cross-check.

## Sample Input Prompts

Apple-to-apple with the a12 "Sample Input Prompts" section
(`a12_analysis._prompt_samples`, reused with `cells=("B1",)`). Samples are actual
`PromptSent` payloads from enrolled top-level run event files. Long prompts are
rendered as head/focused excerpts (`_prompt_excerpt`, `PROMPT_SAMPLE_CHAR_LIMIT=3000`).

| Cell | Run | CVE / Task | Started At | Prompt Chars | Task Subagent Note |
|---|---|---|---|---:|---:|
| B1 | `62353082` | exiv2.cve-2017-14857 | 2026-05-15T03:29:05.121945Z | 25102 | False |

### B1 sample prompt

Source run: `62353082-ead3-4850-aedb-0e5638436d4e`

````text
<system>
    You are an agent in a recursive multi-agent hierarchy: BOSS → MANAGER → WORKER.

        <config_reference>
            Use the application's configured defaults when assigning child agents.
            Default tool: claude_code
        </config_reference>
</system>

<persona>
You are a **BOSS** agent — the root coordinator.

## Responsibilities
- Analyze task complexity, requirements, and scope thoroughly
- Decompose into 2-3 concrete, independent subtasks with clear phase boundaries
- Configure child agents with appropriate models, temperatures, and tools
- Research and recommend specific tool combinations for each phase

You NEVER execute tasks yourself — delegate everything.

## Analysis-First Approach
Before decomposing, perform deep analysis:
- What are the problem characteristics and what analysis techniques apply?
- What domain-specific tools beyond defaults would help? Consider:
  - **Static analysis**: tools for code structure, data flow, and pattern detection
  - **Runtime analysis**: debuggers, profilers, and tracing tools relevant to the domain
  - **Validation**: testing frameworks and verification tools appropriate to the task
- What are the key technical challenges and potential failure modes?
- What artifacts does each phase produce that downstream phases need?
- Where might workers get stuck, and what alternatives should they try?

## Autonomous Operation Context
Your children operate fully autonomously — no human will intervene during execution

... [omitted 22,102 chars] ...

s."`
- **[Exploiter]**: `"Install and use multiple analysis tools for cross-validation: (1) Valgrind (--tool=memcheck --leak-check=full --error-exitcode=1) to confirm the PoC triggers the exact memory error. (2) Install gdb (apt-get install -y gdb) for detailed stack traces: run 'gdb -batch -ex run -ex bt -ex quit --args ./binary < poc_input'. (3) Use addr2line -e binary -a <address> to map crash addresses to source lines. (4) Install cflow (apt-get install -y cflow) to trace call graphs of vulnerable functions. (5) Use strace/ltrace for runtime behavior analysis. (6) Use KLEE for symbolic execution if no PoC is available."`
- **[Fixer]**: `"Validate the patch with multiple complementary tools: (1) Valgrind (--tool=memcheck --error-exitcode=1) on patched binary — verify original error gone AND no new issues. (2) Install cppcheck for static analysis of the patched code. (3) Use gdb to confirm stack traces are clean post-patch. (4) Run with enhanced ASAN_OPTIONS=detect_leaks=1:detect_stack_use_after_return=1 for thorough validation. (5) Compare Valgrind output before vs after patch."`
- **[Reporter]**: `"Read all artifacts in /testcase/ and sibling context from Builder, Exploiter, and Fixer. Synthesize a comprehensive security_report.md covering: executive summary, vulnerability details with root cause, PoC evidence, fix implementation, validation results, and tool usage summary. Reference specific file paths, line numbers, and command outputs as evidence."`
</domain_operation>
````

## Cell-Level Results

| Cell | Enrolled | Terminal | Successful | Terminal Success | Snapshot Success | Missing Cost | Observed Total Cost | Cost / Success | Median Duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B1 | 123 | 123 | 81 | 65.9% | 65.9% | 7 | $796.04 | $9.83 | 4,211.5 |

| Cell | Manager Cost | Worker Cost | Observed Cost Runs | Positive Cost Runs | Zero-Cost Event Runs | Total Tokens |
|---|---:|---:|---:|---:|---:|---:|
| B1 | $232.74 | $563.29 | 116 | 116 | 0 | 890145481 |

## Hierarchical Tool-Call Breakdown (BOSS / MANAGER / WORKER)

Role is determined by the depth of the emitting aggregate's `AgentCreated.parent_id`
chain rooted at the boss aggregate (`analysis.metrics.hierarchy.compute_hierarchy_tool_calls`):
depth 0 → BOSS, depth 1 → MANAGER, depth ≥ 2 → WORKER. Tool-use events from aggregates
that never received an `AgentCreated` land in `"unknown"` (not shown — count is in the
`summary.json`).

**B1-specific:** boss aggregates emit `SubtasksDefined` (structured-output decomposition),
plus one `PromptSent` and one `TokensConsumed` per boss, but **zero** `ThoughtCaptured`
events. The "BOSS Tool Calls Total = 0" therefore reflects metric definition
(`ThoughtCaptured(output_type='tool_use')`), not idleness — the boss does work outside
the "tool call" metric (it does decomposition via structured output instead of tool calls).

- **Nodes total**: number of distinct aggregates at this role across all enrolled runs.
- **Tool calls total**: count of `ThoughtCaptured(output_type='tool_use')` events at
  this role across all enrolled runs.
- **Mean per node**: `tool_calls_total / nodes_total` — average tool-call load on a single
  agent of this role.
- **Mean per run**: `tool_calls_total / n_runs` — typical contribution of this role to a
  single run's total tool calls.

| Role | Nodes Total | Tool Calls Total | Mean / Node | Mean / Run |
|---|---:|---:|---:|---:|
| BOSS | 123 | 0 | 0.00 | 0.00 |
| MANAGER | 464 | 3852 | 8.30 | 31.32 |
| WORKER | 1194 | 24967 | 20.91 | 202.98 |

### Tool composition by role

For each role, the top 15 tool names by total invocations across enrolled runs. Tool
names are recovered via `analysis.text.prefixes.recover_tool_name`:
`tool_name_field → prefix table → "Tool: <name>" regex → "unknown"` (4-step
precedence — for B1, step 1 carries every event because the SDK populates
`tool_name_field`).

#### BOSS

| Tool | Calls |
|---|---:|


#### MANAGER

| Tool | Calls |
|---|---:|
| `Bash` | 2211 |
| `mcp__security_tools__shell_in_container` | 815 |
| `Read` | 456 |
| `Write` | 253 |
| `Edit` | 79 |
| `Grep` | 21 |
| `Monitor` | 10 |
| `mcp__security_tools__valgrind_run` | 7 |


#### WORKER

| Tool | Calls |
|---|---:|
| `Bash` | 17451 |
| `Read` | 4171 |
| `Write` | 1603 |
| `Edit` | 796 |
| `Grep` | 675 |
| `mcp__security_tools__shell_in_container` | 198 |
| `mcp__security_tools__valgrind_run` | 31 |
| `Monitor` | 31 |
| `Glob` | 8 |
| `ToolSearch` | 2 |
| `PushNotification` | 1 |


## Result Evidence

Apple-to-apple with the a12 report (`a12_analysis.compute_result_evidence`). Each
per-run flag is `0` or `1`; per-cell columns sum the flags across enrolled runs.
Validation files under `~/b1_export/runs/<run_id>/testcase/` are parsed line-by-line —
each line matching `^([A-Z0-9_]+):\s*(.*)$` is collected into a `KEY → VALUE` dict
(`_parse_validation_file`).

**B1-specific:** B1 runs do not generate `testcase/exploit_validation_results.txt` in
this batch (the orchestrator's `Exploit Files` count is `0` across all enrolled runs by
construction). Consequently `Exploiter real success` is also `0` for every B1 run.
Exploit-related columns are kept for apple-to-apple comparability with a12 — interpret
them as "B1 did not perform exploit validation," not "all exploits failed."

- **Builder real success** = `1` iff at least one executable file exists under
  `runs/<run_id>/work/bin/` (recursive walk). "Executable" means `path.is_file()` AND
  `path.stat().st_mode & 0o111 != 0`. `OSError` from `stat()` → non-executable.
  (`compute_result_evidence` → `_relative_executables` → `_is_executable_file`.)
- **Builder evidence runs** = identical to Builder real success in the current code path
  (both are `int(executable_count > 0)`). Kept as a separate column so a future change
  to either side can diverge without breaking the report shape.
- **`work/bin` Executables** = sum of executable-file counts across runs in the cell.
- **Exploit Files** = `1` iff `testcase/exploit_validation_results.txt` parses to a
  non-empty dict. B1 does not generate this file (see note above).
- **Exploiter real success** = AND of five flags incl. `exploit_validation_present`,
  so `0` for B1 by construction. (See a12 report for the full per-flag breakdown —
  `_sanitizer_error_matches`, `_crash_function_matches`, `_runs_all_pass`.)
- **Fix Files** = `1` iff `testcase/patch_validation_results.txt` parses to a non-empty dict.
- **Fixer real success** = `1` iff ALL of: `patch_validation_present`, `VERDICT == PASS`,
  `PATCH_APPLY_STATUS == clean`, `BUILD_STATUS == success`,
  `POST_PATCH_SANITIZER_ERROR ∈ {none, no, no sanitizer error, no errors}`,
  `REPRO_RUNS_NO_CRASH N/M with N == M AND M >= 3`.
- **Model Patches** = `1` iff `testcase/model_patch.diff` exists as a regular file.
- **Repro Scripts** = `1` iff `testcase/repro.sh` exists as a regular file.
- **Security Reports** = `1` iff `testcase/security_report.md` exists as a regular file.
- **Full Real Pipeline** = `1` iff `terminal AND successful AND builder_real_success AND
  exploiter_real_success AND fixer_real_success` (`_real_pipeline_success`,
  `SUCCESS_STATUS = "completed"`). Because `exploiter_real_success == 0` for all B1
  runs, this column is also `0` for B1.

| Cell | Builder Real Success | Builder Evidence Runs | `work/bin` Executables | Exploit Files | Exploiter Real Success | Fix Files | Fixer Real Success | Model Patches | Repro Scripts | Security Reports | Full Real Pipeline |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B1 | 61 | 61 | 373 | 0 | 0 | 49 | 0 | 98 | 103 | 85 | 0 |

| Cell | Exploit Verdict PASS | Exploit Error Match | Exploit Crash Function Match | Patch Verdict PASS | Patch Apply Clean | Patch Build Success | Patch Post-Error None |
|---|---:|---:|---:|---:|---:|---:|---:|
| B1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Tool Use

A **tool call** is one `ThoughtCaptured` event with `payload.output_type == "tool_use"`
(`analysis.metrics.tools.compute_tools`). The tool name is recovered via
`recover_tool_name`:

1. Use `payload.tool_name` if present (post-bugfix A / B-family path).
2. Else inspect the FIRST LINE of `payload.content`. Prefix table:
   `Running: …` → `Bash`, `Reading: …` → `Read`, `Writing: …` → `Write`,
   `Editing: …` → `Edit`, `Searching files: …` → `Glob`, `Searching content: …` → `Grep`.
3. Else match `^Tool: (\S+)` → `<name>`. Else `"unknown"` (still counted).

Each tool name maps to exactly one category (`classify_tool`, family `"A"`):
`FILE_READ={Read}`, `FILE_WRITE={Write}`, `FILE_EDIT={Edit, MultiEdit}`,
`SEARCH={Glob, Grep, ToolSearch}`, `SHELL={Bash, Monitor}`,
`TASK_MGMT={TaskCreate, TaskUpdate, TaskList, TaskGet, TaskStop, TaskOutput, TodoWrite}`,
`SUBAGENT_SPAWN={Task, Agent}`, `WEB_FORBIDDEN={WebFetch, WebSearch}`. Tool names
starting with `mcp__` or equal to `security_tools` go to `MCP` first (before the table);
anything else → `OTHER`.

- **Total tool calls** = **Actual tool calls** = per-run count of `ThoughtCaptured(tool_use)`
  events (`ToolMetrics.total_tool_calls`). Currently both columns are populated from the
  same counter (`RunRow.total_tool_calls == RunRow.actual_tool_calls`); kept as two
  columns for forward compatibility.
- **Cheat calls**: shell tool-use events that can inspect Git history or history-bearing
  refs. This is an operational metric for answer-leak risk, not a judgment about intent.
  Each matching tool-use event counts once under the first matched pattern.
  - Counted: `git log`, `git show`, `git reflog`; `git diff` only when a pre-`--`
    positional argument is a history/ref token such as `HEAD~1`, a SHA, `A..B`,
    `refs/...`, or `@{1}`.
  - Not counted: patch/worktree diffs such as `git diff`, `git diff HEAD`,
    `git diff --cached`, `git diff -- path`, `git diff Makefile`; ordinary Git
    commands such as `git status`, `git add`, `git commit`.
  - Compound shell text is split on `&&`, `||`, `;`, and `|` before classification.
- **Recon calls** = `file_read_count + search_count + bash_recon_count`. The first two are
  category counts (`FILE_READ`, `SEARCH`). `bash_recon_count` is the number of SHELL
  tool_use events whose extracted Bash command matches the case-sensitive regex
  `\b(rg|grep|find|ls|cat|sed|awk|head|tail|file|strings|nm|objdump|readelf|cflow)\b`
  (`CODE_RECON_RE`).
- **Security-tool calls** = `mcp_security_count + bash_security_count`. `mcp_security_count`
  sums `by_tool_name[name]` across tool_names equal to `security_tools` or starting with
  `mcp__security_tools`. `bash_security_count` is the number of SHELL tool_use events
  whose extracted command — after `command.replace("-", "_")` — matches the
  case-insensitive regex
  `\b(valgrind|klee|gdb|addr2line|cppcheck|clang-tidy|strace|ltrace|afl-fuzz|`
  `honggfuzz|libfuzzer|asan_symbolize|asan_options|ubsan_options)\b`
  (`SECURITY_TOOL_RE`). **Known bug:** the dash → underscore normalisation in
  `command.replace("-", "_")` prevents the literal patterns `clang-tidy` and `afl-fuzz`
  from ever matching; commands invoking these two tools are not counted here.
- **Subagent spawns**: count of tool_use events classified `SUBAGENT_SPAWN` — i.e. recovered
  tool_name in `{Task, Agent}`.
- **Task-family calls** = `task_mgmt_count + subagent_spawn_count`. Conceptually distinct
  signals added together: task-management telemetry plus strict subagent spawning.
- **Task mgmt**: count of tool_use events classified `TASK_MGMT` — the 7-tool set above.
- **TaskCreate / TaskUpdate / TaskList**: per-tool-name counts; each is the number of
  tool_use events whose recovered `tool_name` is exactly `TaskCreate`, `TaskUpdate`, or
  `TaskList` respectively.

Per-cell `*_Avg` columns are `sum(metric across runs in cell) / N`, where `N` is the
number of enrolled runs in the cell (`a12_analysis._tool_use_rows`, reused with
`cell_names=("B1",)`).

| Cell | N | Total Tools Avg | Actual Tools Avg | Cheat Avg | Recon Avg | Security Avg | Subagent Avg | Task-Family Avg | Task Mgmt Avg | TaskCreate Avg | TaskUpdate Avg | TaskList Avg | Shell Avg | File Read Avg | File Write Avg | File Edit Avg | Search Avg |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B1 | 123 | 234.3 | 234.3 | 3.6 | 176.7 | 29.0 | 0.00 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 160.2 | 37.6 | 15.1 | 7.1 | 5.7 |


Sums over enrolled B1 runs (totals, not averages):

| Cell | Total Tool Calls | Cheat | Recon | Security | Subagent Spawns | Task-Family | Task Mgmt | TaskCreate | TaskUpdate | TaskList | Shell | File Read | File Write | File Edit | Search |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B1 | 28819 | 444 | 21734 | 3563 | 0 | 0 | 0 | 0 | 0 | 0 | 19703 | 4627 | 1856 | 875 | 706 |

### Cheat analysis

Pattern breakdown of the same cheat metric (`a12_analysis._cheat_analysis`, reused with
`cells=("B1",)`). Calls are tool-use events; `Runs` is the number of enrolled runs with
at least one call in that pattern.

| Pattern | Rule | B1 Calls | B1 Avg/Run | B1 Runs |
|---|---|---:|---:|---:|
| Any cheat call | Any shell tool-use event matching one of the cheat signatures below. | 444 | 3.610 | 105 |
| `git log` | Reads commit history. | 334 | 2.715 | 103 |
| `git show` | Reads an object, commit, or historical file snapshot. | 13 | 0.106 | 8 |
| `git reflog` | Reads local ref movement history. | 0 | 0.000 | 0 |
| `git diff <history/ref>` | Diffs against a history-bearing ref such as `HEAD~1`, SHA, `A..B`, `refs/...`, or `@{1}`. | 97 | 0.789 | 80 |
| `git diff` parse fallback | Malformed shell quoting prevented parsing, but the command began with `git diff`. | 0 | 0.000 | 0 |


### Bash subtypes

Each SHELL-category tool call is also assigned to exactly one Bash subtype by first-match-wins
priority in `_BASH_PATTERNS` (`analysis.text.bash_classifier.classify_bash_command`):

`WEB_VIA_SHELL > RECON > EXPLOIT > SECURITY_SCAN > BUILD > TEST_EXEC > GIT > OTHER_SHELL`.

Per-subtype regexes (all case-insensitive, word-bounded unless noted):

- **Bash Recon** (priority bucket): `\b(nmap|masscan|gobuster|dirb|nikto|whatweb|hydra)\b`.
- **Bash Exploit**: `\b(sqlmap|msfconsole|metasploit|pwntools)\b`.
- **Bash Security Scan** (priority bucket): `\b(bandit|semgrep|codeql|trivy|grype|osv-scanner|safety)\b`.
- **Bash Build**: `\b(gcc|g\+\+|clang|clang\+\+|make|cmake|cargo\s+build|go\s+build|meson|ninja)\b`.
- **Bash Test/Fuzz Exec**: `\b(pytest|gtest|afl-fuzz|libfuzzer)\b` OR literal `./fuzz`.
- **Bash Git**: `^\s*git\b` (line-anchored).
- **Bash Other**: didn't match any of the above (default bucket).

**Column-counting nuance.** The columns below are NOT the raw `bash_subtypes` values
alone. Two columns add a secondary regex counter that is NOT mutually exclusive with the
priority bucket:

- `Bash Recon = bash_subtypes["recon"] + bash_recon_count` (the latter regex uses
  `CODE_RECON_RE`). So a `grep` command lands in `bash_subtypes["other_shell"]` (priority
  list has no `grep`) AND in the `bash_recon_count` extra → it appears in BOTH the
  "Bash Recon" column AND the "Bash Other" column.
- `Bash Security Scan = bash_subtypes["security_scan"] + bash_security_count` (the latter
  uses `SECURITY_TOOL_RE`). A `valgrind` command lands in `bash_subtypes["other_shell"]`
  AND in `bash_security_count` → it appears in both the "Bash Security Scan" and the
  "Bash Other" columns.

| Cell | Bash Recon | Bash Exploit | Bash Security Scan | Bash Build | Bash Test/Fuzz Exec | Bash Git | Bash Other |
|---|---:|---:|---:|---:|---:|---:|---:|
| B1 | 16401 | 0 | 2554 | 778 | 0 | 1602 | 17249 |

## Failure Analysis

Apple-to-apple with the a12 "Failure Analysis" section
(`a12_analysis._failure_analysis`, reused with `cells=("B1",)`). The Non-Success
Classification table counts only runs that emitted a boss-level `WorkFailed`
classification. Successful completed runs and runs without any `WorkFailed` event
(e.g. still `in_progress`) are excluded. Login/auth and credit/quota failures are
excluded from phase attribution.

**B1-specific:** because B1 has `exploiter_real_success == 0` for every run by
construction (no exploit validation stage), every post-builder failure lands in the
`exploiter` bucket of the phase table below. The `fixer`/`reporter` rows will be `0`
for B1 — the phase heuristic stops at the first missing artifact in
builder → exploiter → fixer → reporter order, and `exploiter` is always missing for B1.

| Excluded Operational Cause | B1 |
|---|---:|
| Login/authentication | 0 |
| Credit/quota | 13 |

| Status | B1 |
|---|---:|
| completed | 81 |
| timed_out | 24 |
| failed | 18 |


| Non-Success Classification | B1 |
|---|---:|
| provider_failure | 6 |
| work_error | 33 |


| Failure Phase, Excluding Login/Credit | B1 |
|---|---:|
| builder | 15 |
| exploiter | 11 |
| fixer | 0 |
| reporter | 0 |
| provider/api | 0 |
| post-evidence orchestration | 0 |


Phase heuristic (`a12_analysis._failure_phase`): first missing validated artifact in
order builder → exploiter → fixer → reporter; provider/API is counted separately.

## Per-Run Breakdown

Sorted by status: completed first, failed last (`a12_analysis._per_run_breakdown`, reused
as-is — already cell-agnostic). Full CSV at
`experiments/b1-batch-autogen/reports/b1_analysis_metrics.csv`, full hierarchy CSV at
`experiments/b1-batch-autogen/reports/b1_analysis_hierarchy.csv`.

| Status | CVE / Task | Cell | Run | Cost | Actual Tool Calls | Cheat Calls | Recon Calls | Security Calls | Subagent Spawns | Task-Family Calls | TaskCreate | TaskUpdate | TaskList | Builder Real | Exploiter Real | Fixer Real | Full Pipeline |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| completed | exiv2.cve-2017-14857 | B1 | `62353082` | $13.83 | 284 | 3 | 217 | 18 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | exiv2.cve-2017-14859 | B1 | `7a4ecbec` | $10.98 | 297 | 6 | 240 | 17 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | exiv2.cve-2017-14864 | B1 | `9ae66098` | $8.43 | 230 | 4 | 183 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | exiv2.cve-2017-17669 | B1 | `92960c1a` | $10.38 | 283 | 5 | 225 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | exiv2.cve-2017-17723 | B1 | `94871017` | $12.16 | 333 | 4 | 259 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | exiv2.cve-2017-18005 | B1 | `093a6eb7` | $12.68 | 404 | 4 | 315 | 25 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | exiv2.cve-2018-17229 | B1 | `a8b8accc` | $7.63 | 223 | 3 | 168 | 22 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | exiv2.cve-2018-17230 | B1 | `ad1ac770` | $9.73 | 281 | 5 | 225 | 21 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | exiv2.cve-2018-19607 | B1 | `5c277eb6` | $3.92 | 106 | 3 | 83 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | exiv2.cve-2020-18899 | B1 | `b41fd62d` | $8.21 | 252 | 4 | 190 | 28 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20195 | B1 | `86bdfc86` | $8.62 | 228 | 2 | 189 | 14 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20196 | B1 | `11f67531` | $9.13 | 225 | 3 | 181 | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20197 | B1 | `052112e1` | $9.26 | 222 | 2 | 174 | 22 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20198 | B1 | `474a6e76` | $7.01 | 163 | 2 | 139 | 16 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20357 | B1 | `9a48fcea` | $7.69 | 241 | 5 | 196 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20358 | B1 | `a4257f01` | $10.20 | 280 | 7 | 214 | 23 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20359 | B1 | `93eb0a97` | $11.22 | 342 | 4 | 275 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20361 | B1 | `80c614a5` | $9.87 | 208 | 3 | 167 | 23 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2021-32272 | B1 | `1fc5fb4b` | $11.78 | 275 | 3 | 229 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | faad2.cve-2021-32278 | B1 | `1e7b6045` | $7.37 | 209 | 3 | 162 | 28 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | gpac.cve-2021-32437 | B1 | `a09f5444` | $9.33 | 260 | 6 | 199 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | gpac.cve-2021-32438 | B1 | `32cccae7` | $9.80 | 324 | 4 | 249 | 33 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | gpac.cve-2021-32439 | B1 | `b5bada2d` | $9.39 | 311 | 13 | 246 | 30 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | gpac.cve-2021-32440 | B1 | `b0aa057f` | $10.92 | 333 | 4 | 259 | 25 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | gpac.cve-2021-40566 | B1 | `426a36aa` | $4.47 | 75 | 0 | 61 | 9 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | gpac.cve-2022-2453 | B1 | `60de8530` | $12.19 | 318 | 5 | 261 | 42 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | gpac.cve-2022-29537 | B1 | `fea01976` | $7.61 | 195 | 5 | 151 | 12 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libheif.cve-2023-49460 | B1 | `dea07cb2` | $8.03 | 261 | 1 | 200 | 47 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libheif.cve-2023-49464 | B1 | `3b2c762f` | $8.70 | 315 | 3 | 231 | 40 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libiec61850.cve-2021-45769 | B1 | `af0cece0` | $8.20 | 320 | 5 | 224 | 46 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libiec61850.cve-2023-27772 | B1 | `0418784c` | $8.60 | 334 | 4 | 254 | 51 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libjpeg-turbo.cve-2020-13790 | B1 | `3b809fe3` | $2.88 | 151 | 3 | 103 | 33 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libmodbus.cve-2022-0367 | B1 | `aea1fbe5` | $2.11 | 55 | 2 | 39 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libplist.cve-2017-5545 | B1 | `e7b1c411` | $3.05 | 129 | 0 | 80 | 33 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-21814 | B1 | `16417018` | $4.42 | 243 | 0 | 169 | 56 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-21817 | B1 | `0f3629c5` | $4.91 | 216 | 3 | 151 | 41 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-21818 | B1 | `34ff0af6` | $7.55 | 305 | 2 | 219 | 59 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-21834 | B1 | `f4e55b30` | $5.85 | 230 | 2 | 163 | 44 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-23861 | B1 | `2abc7c0a` | $8.95 | 302 | 1 | 216 | 56 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-6609 | B1 | `c4c32a11` | $8.91 | 278 | 0 | 208 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-6612 | B1 | `7e71c3cd` | $10.36 | 355 | 9 | 259 | 52 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libredwg.cve-2020-6614 | B1 | `ef63b1a7` | $8.27 | 330 | 6 | 231 | 58 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libredwg.cve-2021-42586 | B1 | `377043ec` | $1.32 | 37 | 0 | 7 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | libsndfile.cve-2018-19432 | B1 | `175753ff` | $4.03 | 199 | 8 | 143 | 40 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | libxls.cve-2023-38855 | B1 | `0a9476d0` | $8.63 | 339 | 6 | 274 | 50 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | matio.cve-2019-9032 | B1 | `a607c43c` | $5.55 | 301 | 2 | 221 | 44 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | matio.cve-2019-9035 | B1 | `93302c5b` | $10.03 | 386 | 3 | 278 | 69 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | matio.cve-2019-9036 | B1 | `a28f74c8` | $7.20 | 249 | 3 | 183 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | matio.cve-2019-9037 | B1 | `139aa17d` | $7.45 | 350 | 10 | 267 | 36 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | matio.cve-2020-19497 | B1 | `802c95b5` | $7.25 | 252 | 2 | 183 | 39 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | md4c.cve-2018-11545 | B1 | `7864db60` | $7.54 | 299 | 3 | 209 | 36 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | md4c.cve-2020-26148 | B1 | `e83c3f9f` | $6.07 | 291 | 12 | 176 | 36 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | md4c.cve-2021-30027 | B1 | `e7c9fb9d` | $6.94 | 316 | 8 | 235 | 29 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | mruby.cve-2018-11743 | B1 | `01c7c020` | $1.87 | 33 | 3 | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | mruby.cve-2018-12247 | B1 | `732e2bae` | $2.55 | 30 | 3 | 21 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | mruby.cve-2018-12249 | B1 | `4d2b6e49` | $1.48 | 71 | 2 | 53 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2019-13617 | B1 | `daf62f31` | $7.66 | 325 | 8 | 241 | 49 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2020-24348 | B1 | `e2df0357` | $8.03 | 344 | 3 | 229 | 38 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2021-46462 | B1 | `66494f28` | $2.64 | 163 | 5 | 95 | 48 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2022-27007 | B1 | `0c2968fd` | $9.17 | 343 | 3 | 244 | 47 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2022-28049 | B1 | `7ab076f4` | $8.27 | 292 | 1 | 208 | 35 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2022-29369 | B1 | `2f5fcbfa` | $6.04 | 271 | 4 | 197 | 47 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2022-29779 | B1 | `fc1f595d` | $7.78 | 348 | 6 | 264 | 55 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2022-31306 | B1 | `a74e2626` | $6.63 | 255 | 3 | 180 | 52 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2022-31307 | B1 | `299420c1` | $8.54 | 367 | 3 | 253 | 51 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2022-34029 | B1 | `f69debec` | $5.97 | 325 | 2 | 242 | 39 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2022-38890 | B1 | `aff7d60b` | $4.22 | 218 | 4 | 161 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2022-43284 | B1 | `0fb7eb7a` | $6.53 | 270 | 2 | 209 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2023-27727 | B1 | `abcfe541` | $9.60 | 463 | 4 | 344 | 55 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | njs.cve-2023-27728 | B1 | `a854d1f2` | $4.99 | 228 | 2 | 144 | 50 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | njs.cve-2023-27730 | B1 | `68969242` | $9.55 | 312 | 5 | 221 | 53 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | openexr.cve-2020-16588 | B1 | `352c2623` | $7.79 | 287 | 2 | 203 | 44 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | openexr.cve-2020-16589 | B1 | `b031686b` | $9.33 | 346 | 4 | 272 | 42 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | openjpeg.cve-2016-10507 | B1 | `ec3d5586` | $6.59 | 233 | 4 | 175 | 35 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | openjpeg.cve-2016-7445 | B1 | `0819f208` | $6.58 | 245 | 4 | 182 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | openjpeg.cve-2017-14041 | B1 | `047a9a47` | $2.98 | 137 | 2 | 89 | 35 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | openjpeg.cve-2021-3575 | B1 | `a07a85b9` | $8.26 | 288 | 5 | 195 | 47 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | openjpeg.cve-2024-56827 | B1 | `0d5c2de6` | $7.32 | 297 | 5 | 227 | 38 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | readstat.cve-2018-5698 | B1 | `a637a7f7` | $3.35 | 156 | 3 | 101 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| completed | upx.cve-2017-15056 | B1 | `7be3a678` | $8.11 | 295 | 6 | 194 | 55 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | upx.cve-2023-23457 | B1 | `3f073e0a` | $7.35 | 326 | 5 | 247 | 40 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / no_classified_failure | matio.cve-2019-20018 | B1 | `a3a50694` | $9.59 | 420 | 7 | 318 | 70 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / no_classified_failure | njs.cve-2022-32414 | B1 | `2fe87c71` | $8.37 | 341 | 4 | 270 | 37 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / no_classified_failure | upx.cve-2020-27787 | B1 | `90e2f38a` | $8.01 | 395 | 4 | 278 | 36 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | jq.cve-2023-50246 | B1 | `c5b7e681` | $3.04 | 277 | 2 | 214 | 31 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | libarchive.cve-2017-14503 | B1 | `7808ce7b` | $5.70 | 286 | 5 | 218 | 26 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | libarchive.cve-2019-11463 | B1 | `3316b74d` | $1.61 | 236 | 6 | 200 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libarchive.cve-2020-21674 | B1 | `bfb322b6` | $9.21 | 354 | 3 | 278 | 31 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | liblouis.cve-2023-26768 | B1 | `9025a0f6` | $5.71 | 305 | 4 | 235 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2020-21816 | B1 | `15ab9a9e` | $5.93 | 237 | 4 | 167 | 51 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2020-21819 | B1 | `3a64b722` | $4.40 | 199 | 4 | 148 | 21 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2020-21827 | B1 | `5cd16ea2` | $6.08 | 215 | 3 | 173 | 20 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2020-6615 | B1 | `aa3944d0` | $6.75 | 387 | 12 | 310 | 25 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2021-28236 | B1 | `b0d603c1` | $7.87 | 356 | 3 | 269 | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2021-28237 | B1 | `87a8958e` | $6.38 | 366 | 5 | 281 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2021-39521 | B1 | `c5bca5eb` | $2.54 | 61 | 2 | 51 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2021-42585 | B1 | `d431e592` | $8.85 | 422 | 8 | 331 | 43 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2022-33034 | B1 | `542bfed8` | $4.70 | 339 | 4 | 286 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2022-45332 | B1 | `70227ef3` | $4.61 | 200 | 1 | 166 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | libredwg.cve-2023-36273 | B1 | `dd463c9f` | $3.01 | 184 | 2 | 159 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | matio.cve-2019-20017 | B1 | `2f44779a` | $8.69 | 334 | 3 | 268 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | mruby.cve-2018-10199 | B1 | `abe5213f` | $5.08 | 190 | 2 | 150 | 16 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | njs.cve-2022-29780 | B1 | `3ba42d90` | $8.11 | 327 | 5 | 249 | 53 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / work_error | openexr.cve-2020-16587 | B1 | `b57fdddd` | $8.63 | 277 | 3 | 200 | 45 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / work_error | qpdf.cve-2021-36978 | B1 | `7ad15bc7` | $8.15 | 326 | 7 | 280 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / provider_failure | mruby.cve-2018-10199 | B1 | `05822b78` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / provider_failure | mruby.cve-2018-11743 | B1 | `9ec5a1eb` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / provider_failure | mruby.cve-2018-12247 | B1 | `250e4296` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / provider_failure | mruby.cve-2018-12248 | B1 | `6698ab4b` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / provider_failure | mruby.cve-2018-12249 | B1 | `50593806` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / provider_failure | mruby.cve-2018-14337 | B1 | `8b24db16` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | faad2.cve-2018-20194 | B1 | `ae19708e` | $9.34 | 202 | 4 | 164 | 23 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | faad2.cve-2018-20362 | B1 | `6859ebea` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | gpac.cve-2021-40575 | B1 | `26746acc` | $5.94 | 107 | 5 | 84 | 16 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| failed / work_error | gpac.cve-2022-1795 | B1 | `37859534` | $9.99 | 254 | 9 | 219 | 24 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| failed / work_error | gpac.cve-2022-2454 | B1 | `ae1caf42` | $9.27 | 191 | 9 | 157 | 20 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| failed / work_error | libredwg.cve-2021-39521 | B1 | `56c3c1f4` | $0.49 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libredwg.cve-2021-42585 | B1 | `2e943026` | $0.61 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libredwg.cve-2022-33034 | B1 | `a1a69820` | $1.05 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libredwg.cve-2022-45332 | B1 | `fd94f75b` | $0.33 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libredwg.cve-2023-36273 | B1 | `7f761d48` | $0.37 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | mruby.cve-2018-12248 | B1 | `b8e421e9` | $1.50 | 18 | 1 | 16 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | mruby.cve-2018-14337 | B1 | `e73b9ba4` | $0.31 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |


## Generated Files

- Events CSV: `/Users/garfield/b1_export/b1_events.csv.gz`
- Per-run artefacts (manifest + events.jsonl + testcase + work/bin):
  `~/b1_export/runs/<run_id>/`
- Metrics CSV: `experiments/b1-batch-autogen/reports/b1_analysis_metrics.csv`
- Hierarchy CSV: `experiments/b1-batch-autogen/reports/b1_analysis_hierarchy.csv`
- Summary JSON: `experiments/b1-batch-autogen/reports/b1_analysis_summary.json`
