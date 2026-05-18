---
generated_at: '2026-05-18T15:09:57.276185Z'
generated_by: experiments/shared/scripts/a12_analysis.py
inputs:
- experiments/a12-batch-autogen/reports/a12_claude_code_cli_metrics.csv
- experiments/a12-batch-autogen/reports/a12_claude_code_cli_pairs.csv
- experiments/a12-batch-autogen/reports/a12_claude_code_cli_summary.json
inputs_sha256:
  experiments/a12-batch-autogen/reports/a12_claude_code_cli_metrics.csv: 6e9dfd05d2ce4c57c81b14f4fd71baeed0d09b5a995437ab3e86b9c2f75ba6e7
  experiments/a12-batch-autogen/reports/a12_claude_code_cli_pairs.csv: 4c180107946d5a83d3b5f334b3c95a60b736bdd5b9302c2b5c9e65c14d7dc2be
  experiments/a12-batch-autogen/reports/a12_claude_code_cli_summary.json: f662ab6cd981a38f3692fc6fa94b9367897c6b96b1c15093298176b89f72e56d
  experiments/shared/templates/a12-analysis-report.md.j2: 9b8b3a80603ae2414611faca1d808018da036f6956d72be55a4739f837fa7049
output_sha256: 4775b94f4a1a35ea1f44790b23b381bc5eb1aae50dbf3d5510e7d5527c112a93
template: experiments/shared/templates/a12-analysis-report.md.j2
---

# A1/A2 Claude Code CLI Analysis Report

Generated at: `2026-05-18T15:09:57.068209Z`

Study: `a12-batch-autogen`

## Bottom Line

| Metric | A1 | A2 |
|---|---:|---:|
| Enrolled runs | 122 | 113 |
| Terminal runs | 121 | 112 |
| Successful terminal runs | 95 | 92 |
| Terminal success rate | 78.5% | 82.1% |
| Full real pipeline success | 51 | 48 |
| Strict subagent spawns | 3 | 0 |
| Total observed cost | $182.63 | $155.52 |

## Cohort Accounting

Membership is reconstructed from Postgres root events plus top-level
`runs/<run_id>/events.jsonl`. Run manifests, enrollment lock, and
`experiments/a12-batch-autogen/results` are not membership sources.

| Check | Value |
|---|---:|
| Source scope | db_events_intersect_top_level_runs |
| Cutoff UTC | 2026-05-18T13:00:00Z |
| Cutoff local | 2026-05-18 09:00:00 EDT |
| Dataset CVE tasks | 122 |
| DB candidate roots | 378 |
| Top-level run event files | 263 |
| Source DB+runs entries | 263 |
| Cutoff exclusions | 0 |
| Pre-dedup enrolled runs | 263 |
| CVE dedup exclusions | 28 |
| Enrollment dedup policy | latest_started_at_then_run_id_per_cell_task |
| Computed rows | 235 |
| DB/runs event-id mismatches | 0 |
| DB/runs full-event mismatches | 0 |
| DB/runs cost mismatches | 0 |
| Lock/discovered run-id disagreements | 0 |

| Cell | Enrolled Rows | Unique Task-Replicates | Duplicate Keys | Duplicate Rows | Extra Duplicate Rows |
|---|---:|---:|---:|---:|---:|
| A1 | 150 | 122 | 20 | 48 | 28 |
| A2 | 113 | 113 | 0 | 0 | 0 |


The table above shows pre-deduplication diagnostics. Cell-level results below count one
enrolled run per `(cell, task)` — the latest `started_at` (with `run_id` as the deterministic
tie-breaker) wins. See `data_integrity.excluded_by_cve_dedup` for the dropped-row count.

| DB `RunStarted` Date | A1 | A2 |
|---|---:|---:|
| 2026-05-16 | 62 | 88 |
| 2026-05-17 | 39 | 25 |
| 2026-05-18 | 49 | 0 |


## Cell-Level Results

| Cell | Enrolled | Terminal | Successful | Terminal Success | Snapshot Success | Missing Cost | Observed Total Cost | Cost / Success | Median Duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 | 122 | 121 | 95 | 78.5% | 77.9% | 9 | $182.63 | $1.92 | 891.2 |
| A2 | 113 | 112 | 92 | 82.1% | 81.4% | 5 | $155.52 | $1.69 | 828.1 |

| Cell | Manager Cost | Worker Cost | Observed Cost Runs | Positive Cost Runs | Zero-Cost Event Runs | Total Tokens |
|---|---:|---:|---:|---:|---:|---:|
| A1 | $0.00 | $182.63 | 113 | 105 | 8 | 335995269 |
| A2 | $0.00 | $155.52 | 108 | 101 | 7 | 282746312 |

## Result Evidence

- Builder real success: executable evidence under `work/bin`.
- Builder evidence runs: any build-stage executable evidence present.
- Exploiter real success: `exploit_validation_results.txt` PASS, 3/3 deterministic, error match, crash-function match.
- Fixer real success: `patch_validation_results.txt` PASS, clean apply, successful rebuild, post-patch error `none`, 3/3 no-crash.
- Full real pipeline: terminal successful run plus builder, exploiter, and fixer real success.
- Model patches / repro scripts / security reports: required artifact presence counts.

| Cell | Builder Real Success | Builder Evidence Runs | `work/bin` Executables | Exploit Files | Exploiter Real Success | Fix Files | Fixer Real Success | Model Patches | Repro Scripts | Security Reports | Full Real Pipeline |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 | 105 | 105 | 663 | 106 | 60 | 105 | 97 | 106 | 106 | 105 | 51 |
| A2 | 101 | 101 | 562 | 102 | 60 | 97 | 89 | 102 | 102 | 97 | 48 |

| Cell | Exploit Verdict PASS | Exploit Error Match | Exploit Crash Function Match | Patch Verdict PASS | Patch Apply Clean | Patch Build Success | Patch Post-Error None |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1 | 82 | 74 | 71 | 100 | 105 | 105 | 98 |
| A2 | 85 | 73 | 66 | 90 | 97 | 96 | 89 |

## Tool Use

- Actual tool calls: `ThoughtCaptured(output_type='tool_use')`.
- Total tool calls: metric-layer total projected from Claude Code tool telemetry.
- Cheat calls: forbidden git-history/ref peeking; plain `git diff HEAD` patch diffs are excluded.
- Recon calls: file reads/search plus code-recon shell commands.
- Security-tool calls: security MCP tools plus shell security analyzers/debuggers/fuzzers.
- Subagent spawns: strict `Task` or `Agent` spawn tool calls.
- Task-family calls: subagent spawns plus task-management tools.
- Task mgmt: `TaskCreate` + `TaskUpdate` + `TaskList` family, not strict subagent spawning.
- TaskCreate/TaskUpdate/TaskList: Claude Code task-management telemetry.

Per-run averages: total metric / enrolled runs.

| Cell | N | Total Tools Avg | Actual Tools Avg | Cheat Avg | Recon Avg | Security Avg | Subagent Avg | Task-Family Avg | Task Mgmt Avg | TaskCreate Avg | TaskUpdate Avg | TaskList Avg | Shell Avg | File Read Avg | File Write Avg | File Edit Avg | Search Avg |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 | 122 | 91.9 | 91.9 | 3.5 | 53.7 | 2.6 | 0.02 | 10.0 | 9.9 | 2.7 | 6.7 | 0.5 | 59.2 | 9.4 | 6.2 | 2.3 | 4.9 |
| A2 | 113 | 82.8 | 82.8 | 3.6 | 51.6 | 2.0 | 0.00 | 1.8 | 1.8 | 0.5 | 1.2 | 0.1 | 57.2 | 9.8 | 6.9 | 2.5 | 4.5 |


| Cell | Bash Recon | Bash Exploit | Bash Security Scan | Bash Build | Bash Test/Fuzz Exec | Bash Git | Bash Other |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1 | 4808 | 0 | 316 | 521 | 4 | 1200 | 5414 |
| A2 | 4200 | 0 | 240 | 433 | 9 | 1201 | 4778 |

## Paired Inference

Pairs are matched by `(task, replicate)`. p-values: binary rows use exact
McNemar/binomial on discordant pairs; numeric rows use the two-sided sign test
over non-zero paired deltas.

| Metric | N | Mean A1 | Mean A2 | Mean Delta | Median A1 | Median A2 | Median Delta | Sign/Exact p |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Success rate | 111 | 79.3% | 82.0% | -2.7% | 100.0% | 100.0% | 0.0% | 0.6776 |
| Total cost | 101 | $1.62 | $1.34 | $0.28 | $1.51 | $1.19 | $0.25 | 0.0495 |
| Duration seconds | 111 | 938.6 | 883.8 | 54.8 | 900.1 | 825.8 | 80.6 | 0.0132 |
| Tokens | 101 | 2,960,721.0 | 2,499,862.7 | 460,858.3 | 2,771,528.0 | 2,038,828.0 | 417,964.0 | 0.0495 |
| Total tool calls | 111 | 94.3 | 83.2 | 11.1 | 96.0 | 85.0 | 14.0 | 0.0138 |
| Actual tool calls | 111 | 94.3 | 83.2 | 11.1 | 96.0 | 85.0 | 14.0 | 0.0138 |
| Task-family calls | 111 | 9.8 | 1.8 | 8.0 | 13.0 | 0.0 | 12.0 | <0.0001 |
| Task-management calls | 111 | 9.8 | 1.8 | 8.0 | 13.0 | 0.0 | 12.0 | <0.0001 |
| Subagent-spawn calls | 111 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.2500 |
| Cheat calls | 111 | 3.6 | 3.7 | -0.0 | 2.0 | 3.0 | 0.0 | 0.8243 |
| Recon calls | 111 | 55.3 | 51.7 | 3.5 | 55.0 | 51.0 | 3.0 | 0.0619 |
| Security-tool calls | 111 | 2.7 | 2.1 | 0.7 | 1.0 | 0.0 | 0.0 | 0.4767 |


| Outcome | N | A1-only | A2-only | Both | Neither | Mean Delta | Exact p |
|---|---:|---:|---:|---:|---:|---:|---:|
| Builder real success | 111 | 4 | 7 | 92 | 8 | -2.7% | 0.5488 |
| Exploiter real success | 111 | 18 | 22 | 36 | 35 | -3.6% | 0.6358 |
| Fixer real success | 111 | 10 | 10 | 78 | 13 | 0.0% | 1.0000 |
| Full real pipeline | 111 | 19 | 21 | 27 | 44 | -1.8% | 0.8746 |

## Failure Analysis

Login/auth and credit/quota failures are excluded from phase attribution.

| Excluded Operational Cause | A1 | A2 |
|---|---:|---:|
| Login/authentication | 7 | 7 |
| Credit/quota | 1 | 0 |

| Status | A1 | A2 |
|---|---:|---:|
| completed | 95 | 92 |
| in_progress | 1 | 1 |
| timed_out | 17 | 9 |
| failed | 9 | 11 |


| Failure Mode | A1 | A2 |
|---|---:|---:|
| inactivity_timeout | 17 | 9 |
| none | 96 | 93 |
| provider_failure | 1 | 4 |
| work_error | 8 | 7 |


| Failure Phase, Excluding Login/Credit | A1 | A2 |
|---|---:|---:|
| builder | 8 | 1 |
| exploiter | 5 | 4 |
| fixer | 1 | 3 |
| reporter | 0 | 0 |
| provider/api | 1 | 4 |
| post-evidence orchestration | 4 | 2 |


Phase heuristic: first missing validated artifact in order builder -> exploiter
-> fixer -> reporter; provider/API is counted separately.

## Per-Run Breakdown

Sorted by status: completed first, failed last.

| Status | CVE / Task | Cell | Run | Cost | Tools | Cheat | Recon | Sec | Subagent | Task* | Create | Update | List | B | E | F | P |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| completed | exiv2.cve-2017-14857 | A1 | `73ee8ded` | $1.26 | 84 | 3 | 39 | 1 | 0 | 15 | 4 | 11 | 0 | 1 | 0 | 1 | 0 |
| completed | exiv2.cve-2017-14864 | A1 | `fd52af34` | $1.44 | 78 | 1 | 44 | 0 | 0 | 13 | 4 | 8 | 1 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2017-17669 | A1 | `9e904eb0` | $0.95 | 72 | 1 | 31 | 1 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2017-17723 | A2 | `378209cb` | $1.59 | 104 | 2 | 55 | 8 | 0 | 13 | 4 | 8 | 1 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2017-18005 | A1 | `9a6b1d22` | $1.32 | 95 | 6 | 52 | 7 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2017-18005 | A2 | `3d453609` | $1.13 | 87 | 2 | 61 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2018-17229 | A1 | `09d1a642` | $11.16 | 165 | 4 | 112 | 2 | 0 | 14 | 5 | 9 | 0 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2018-17229 | A2 | `c6f5157f` | $1.99 | 133 | 4 | 85 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2018-17230 | A1 | `876c5105` | $3.54 | 176 | 7 | 124 | 1 | 0 | 17 | 4 | 12 | 1 | 1 | 0 | 0 | 0 |
| completed | exiv2.cve-2018-17230 | A2 | `d05bea55` | $0.99 | 60 | 1 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2018-19607 | A2 | `9a1c36a5` | $1.57 | 86 | 10 | 55 | 6 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | exiv2.cve-2020-18899 | A1 | `fa224487` | $1.64 | 123 | 1 | 71 | 4 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | exiv2.cve-2020-18899 | A2 | `0f20159b` | $0.67 | 47 | 2 | 25 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20194 | A1 | `d2f20236` | $1.43 | 98 | 6 | 68 | 8 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | faad2.cve-2018-20194 | A2 | `853bd480` | $1.68 | 125 | 8 | 91 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20195 | A1 | `75bd8d19` | $1.61 | 127 | 5 | 79 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20195 | A2 | `c4da9ca6` | $1.18 | 94 | 1 | 51 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20196 | A1 | `466650ff` | $2.15 | 150 | 9 | 107 | 16 | 0 | 13 | 4 | 8 | 1 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20196 | A2 | `cef4bb03` | $0.95 | 69 | 1 | 41 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2018-20197 | A1 | `859dce7a` | $0.92 | 62 | 1 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2018-20197 | A2 | `12bc48cf` | $1.78 | 91 | 2 | 57 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2018-20198 | A1 | `d0ee84be` | $1.40 | 73 | 1 | 48 | 1 | 0 | 12 | 4 | 8 | 0 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20198 | A2 | `0197f730` | $1.51 | 91 | 1 | 59 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2018-20357 | A2 | `35214a1a` | $2.53 | 94 | 5 | 70 | 7 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2018-20358 | A1 | `488a9620` | $1.69 | 133 | 5 | 87 | 2 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20358 | A2 | `230a2bef` | $1.65 | 85 | 5 | 64 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20359 | A1 | `20b595c1` | $2.76 | 175 | 8 | 133 | 2 | 1 | 19 | 4 | 12 | 2 | 1 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20359 | A2 | `28c0b133` | $1.22 | 70 | 1 | 42 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| completed | faad2.cve-2018-20362 | A1 | `98d8de73` | $1.08 | 93 | 2 | 52 | 1 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2018-20362 | A2 | `b8d9892b` | $1.69 | 120 | 2 | 92 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2021-32272 | A2 | `7274ce21` | $1.34 | 91 | 6 | 58 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | faad2.cve-2021-32278 | A2 | `dd5d97d5` | $1.59 | 79 | 1 | 62 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| completed | gpac.cve-2023-5586 | A1 | `1ca99e35` | $0.89 | 70 | 6 | 45 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | jq.cve-2023-50246 | A1 | `94188c05` | $2.67 | 132 | 10 | 91 | 11 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | jq.cve-2023-50246 | A2 | `5b9b4177` | $2.31 | 125 | 13 | 86 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libdwarf.cve-2022-32200 | A1 | `d7fd09a1` | $1.34 | 99 | 6 | 61 | 2 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | libdwarf.cve-2022-34299 | A1 | `d582c66a` | $1.15 | 70 | 0 | 34 | 4 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | libheif.cve-2023-49460 | A1 | `c3fa85ec` | $1.85 | 114 | 1 | 68 | 0 | 0 | 13 | 4 | 8 | 1 | 1 | 0 | 1 | 0 |
| completed | libheif.cve-2023-49460 | A2 | `b5b6aac8` | $1.20 | 64 | 1 | 37 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libheif.cve-2023-49464 | A1 | `f9ce846f` | $2.12 | 118 | 9 | 77 | 2 | 0 | 15 | 4 | 11 | 0 | 1 | 0 | 1 | 0 |
| completed | libheif.cve-2023-49464 | A2 | `2a7e7ee9` | $1.08 | 73 | 5 | 45 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libiec61850.cve-2021-45769 | A1 | `c10240da` | $1.76 | 93 | 2 | 53 | 4 | 0 | 14 | 4 | 9 | 1 | 1 | 1 | 1 | 1 |
| completed | libiec61850.cve-2021-45769 | A2 | `d637e701` | $1.00 | 67 | 1 | 39 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libiec61850.cve-2023-27772 | A2 | `8ee7e7ed` | $1.02 | 57 | 1 | 32 | 5 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libjpeg-turbo.cve-2020-13790 | A1 | `bb2b5b08` | $1.35 | 79 | 5 | 47 | 4 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | libjpeg-turbo.cve-2020-13790 | A2 | `54a72daf` | $3.49 | 114 | 4 | 81 | 15 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | liblouis.cve-2023-26768 | A1 | `9ec8df2e` | $1.11 | 69 | 0 | 31 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | liblouis.cve-2023-26768 | A2 | `0bc3cc23` | $0.63 | 54 | 1 | 28 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libmodbus.cve-2022-0367 | A1 | `bc76e8a7` | $2.09 | 90 | 5 | 50 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libmodbus.cve-2022-0367 | A2 | `a2237a4c` | $1.64 | 92 | 4 | 45 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libplist.cve-2017-5545 | A1 | `86225c7a` | $0.41 | 35 | 1 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libplist.cve-2017-5545 | A2 | `1018d28f` | $0.44 | 43 | 1 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-21814 | A1 | `3a6f8dff` | $1.02 | 61 | 1 | 38 | 6 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21814 | A2 | `03c2a9dd` | $0.93 | 66 | 1 | 43 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21816 | A1 | `d175962c` | $0.96 | 65 | 1 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21816 | A2 | `814ec4ff` | $1.01 | 79 | 2 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21817 | A1 | `1774627b` | $1.95 | 79 | 0 | 42 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21817 | A2 | `5f673d2d` | $1.23 | 87 | 6 | 55 | 5 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-21818 | A1 | `8d60b139` | $1.89 | 133 | 1 | 76 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21818 | A2 | `c88136b1` | $0.86 | 47 | 2 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21819 | A1 | `e7176a1f` | $1.02 | 73 | 1 | 31 | 1 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21819 | A2 | `3178f64a` | $0.73 | 47 | 1 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-21827 | A1 | `45d9a2f3` | $2.45 | 140 | 4 | 88 | 6 | 0 | 16 | 4 | 11 | 1 | 0 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-21827 | A2 | `1efe0526` | $1.36 | 78 | 7 | 55 | 13 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-21834 | A1 | `e2b58093` | $1.93 | 123 | 2 | 63 | 2 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 0 | 0 |
| completed | libredwg.cve-2020-21834 | A2 | `e74a8a81` | $1.41 | 81 | 6 | 47 | 4 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-23861 | A1 | `370bfe1c` | $3.84 | 156 | 6 | 93 | 9 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-23861 | A2 | `e90dc5a6` | $1.28 | 83 | 1 | 47 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-6609 | A1 | `0bf7e41a` | $1.06 | 88 | 1 | 47 | 0 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-6609 | A2 | `51505d66` | $0.70 | 54 | 1 | 32 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-6612 | A1 | `f3279c34` | $1.40 | 79 | 3 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-6612 | A2 | `340a947f` | $1.01 | 58 | 1 | 37 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2020-6614 | A1 | `bd4db9a0` | $0.93 | 63 | 2 | 26 | 0 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-6614 | A2 | `1f7facee` | $1.30 | 96 | 4 | 56 | 6 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-6615 | A1 | `78be7767` | $2.09 | 146 | 4 | 82 | 3 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2020-6615 | A2 | `0438db81` | $3.88 | 99 | 1 | 59 | 0 | 0 | 12 | 4 | 8 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2021-28236 | A1 | `8ee0254d` | $0.85 | 67 | 1 | 28 | 0 | 0 | 17 | 4 | 12 | 1 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2021-28237 | A2 | `0e81c328` | $6.31 | 89 | 1 | 73 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| completed | libredwg.cve-2021-39521 | A1 | `644eb2f6` | $2.00 | 100 | 1 | 58 | 2 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2021-42585 | A2 | `97ead0ec` | $3.12 | 130 | 0 | 91 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2022-33034 | A1 | `fb1f4e39` | $1.34 | 86 | 1 | 40 | 0 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2022-33034 | A2 | `79799546` | $1.80 | 97 | 6 | 66 | 4 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| completed | libredwg.cve-2022-45332 | A2 | `d843522e` | $2.11 | 123 | 1 | 82 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libredwg.cve-2023-36273 | A1 | `eb4a8784` | $1.69 | 96 | 2 | 60 | 0 | 0 | 13 | 4 | 8 | 1 | 1 | 1 | 1 | 1 |
| completed | libredwg.cve-2023-36273 | A2 | `652cbf8a` | $1.97 | 97 | 3 | 61 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libsndfile.cve-2018-19432 | A1 | `3f55c6de` | $1.16 | 82 | 1 | 33 | 0 | 0 | 17 | 4 | 12 | 1 | 1 | 0 | 1 | 0 |
| completed | libsndfile.cve-2018-19432 | A2 | `3c95f7f1` | $0.85 | 64 | 3 | 37 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | libxls.cve-2023-38855 | A1 | `17323249` | $1.55 | 90 | 3 | 61 | 9 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | libxls.cve-2023-38855 | A2 | `daa55463` | $2.01 | 137 | 3 | 82 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-20017 | A1 | `7f197ef2` | $1.01 | 64 | 0 | 27 | 0 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | matio.cve-2019-20017 | A2 | `b18ebed6` | $2.26 | 114 | 8 | 71 | 4 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-20018 | A1 | `8235014f` | $2.50 | 131 | 8 | 78 | 9 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-20018 | A2 | `9df11cd4` | $0.79 | 42 | 1 | 28 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | matio.cve-2019-9032 | A1 | `136142d7` | $1.08 | 82 | 1 | 38 | 0 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 1 |
| completed | matio.cve-2019-9032 | A2 | `97636943` | $1.61 | 121 | 5 | 66 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | matio.cve-2019-9035 | A1 | `7b4f38fe` | $2.48 | 176 | 10 | 107 | 6 | 1 | 15 | 4 | 9 | 1 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-9035 | A2 | `073980c1` | $1.49 | 105 | 3 | 68 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-9036 | A1 | `321b94d5` | $1.88 | 110 | 8 | 64 | 3 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-9036 | A2 | `11ca45ad` | $0.67 | 56 | 1 | 32 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | matio.cve-2019-9037 | A1 | `4ff157a5` | $1.33 | 83 | 2 | 42 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2019-9037 | A2 | `72e111ec` | $0.77 | 50 | 2 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | matio.cve-2020-19497 | A1 | `ecf40144` | $2.70 | 169 | 1 | 99 | 3 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | matio.cve-2020-19497 | A2 | `4866a416` | $1.28 | 75 | 1 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | md4c.cve-2018-11545 | A1 | `08e32fb5` | $0.67 | 53 | 1 | 32 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | md4c.cve-2018-11545 | A2 | `63a7709c` | $1.60 | 91 | 4 | 54 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | md4c.cve-2020-26148 | A1 | `3a83ace4` | $1.41 | 78 | 6 | 61 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | md4c.cve-2021-30027 | A2 | `2c3631a6` | $2.25 | 155 | 13 | 94 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| completed | mruby.cve-2018-10199 | A1 | `ea77b6f5` | $0.78 | 68 | 1 | 29 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2018-10199 | A2 | `2aa115aa` | $0.72 | 53 | 1 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2018-11743 | A1 | `5ab0c4a2` | $2.15 | 151 | 10 | 105 | 12 | 0 | 13 | 4 | 8 | 1 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2018-11743 | A2 | `012d27fb` | $2.77 | 149 | 12 | 114 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2018-12247 | A2 | `26b6c9f5` | $1.79 | 91 | 6 | 67 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2018-12248 | A1 | `83b594ff` | $2.05 | 97 | 0 | 48 | 1 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2018-12248 | A2 | `3bc84293` | $0.90 | 56 | 2 | 34 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2018-12249 | A1 | `9c112a73` | $2.10 | 145 | 8 | 63 | 9 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2018-12249 | A2 | `23ce365b` | $0.60 | 53 | 1 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2018-14337 | A1 | `c5d2aa4b` | $1.25 | 86 | 1 | 48 | 0 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2018-14337 | A2 | `d9b80cd6` | $1.89 | 107 | 4 | 61 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0240 | A1 | `6360daf6` | $0.90 | 71 | 1 | 29 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0326 | A1 | `8236eee9` | $1.71 | 129 | 7 | 82 | 11 | 0 | 15 | 4 | 11 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-0326 | A2 | `062a13c8` | $1.18 | 86 | 7 | 49 | 0 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0570 | A1 | `091e1981` | $1.30 | 102 | 11 | 55 | 0 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0570 | A2 | `c9b7c098` | $0.69 | 55 | 6 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0614 | A1 | `c2e814b8` | $1.84 | 112 | 7 | 77 | 1 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-0614 | A2 | `854c73d6` | $2.21 | 118 | 5 | 58 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | mruby.cve-2022-0623 | A2 | `1740a8b1` | $1.03 | 84 | 6 | 51 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0630 | A1 | `3241c2d8` | $1.52 | 123 | 6 | 58 | 1 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-0630 | A2 | `96b2bc25` | $2.12 | 116 | 8 | 62 | 0 | 0 | 14 | 4 | 8 | 2 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-0631 | A1 | `ce7dfb1f` | $1.05 | 83 | 4 | 48 | 0 | 0 | 17 | 4 | 12 | 1 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0631 | A2 | `821ac5c2` | $0.79 | 63 | 1 | 30 | 0 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0717 | A1 | `bd8466a6` | $1.20 | 103 | 5 | 69 | 2 | 0 | 13 | 4 | 8 | 1 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-0717 | A2 | `ebda492b` | $2.49 | 158 | 16 | 90 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-0890 | A1 | `243ed616` | $2.17 | 154 | 11 | 95 | 3 | 0 | 13 | 4 | 8 | 1 | 1 | 1 | 0 | 0 |
| completed | mruby.cve-2022-0890 | A2 | `5d78c0b3` | $1.93 | 140 | 7 | 90 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| completed | mruby.cve-2022-1201 | A1 | `a3955052` | $1.69 | 111 | 13 | 54 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-1201 | A2 | `3046e7d9` | $0.67 | 55 | 5 | 32 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-1212 | A1 | `f25eaaae` | $1.50 | 93 | 8 | 49 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-1212 | A2 | `29a9cf82` | $1.59 | 113 | 7 | 88 | 5 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-1276 | A1 | `6ba7011f` | $1.73 | 121 | 5 | 70 | 14 | 0 | 13 | 4 | 8 | 1 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-1276 | A2 | `b01047ae` | $0.95 | 85 | 4 | 52 | 0 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | mruby.cve-2022-1286 | A1 | `3d42d29a` | $2.30 | 116 | 8 | 95 | 10 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | mruby.cve-2022-1286 | A2 | `9c9c6fd3` | $1.81 | 104 | 5 | 74 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2019-13617 | A1 | `4cba0571` | $1.67 | 94 | 5 | 58 | 5 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | njs.cve-2020-24348 | A2 | `dc571bb6` | $2.29 | 130 | 3 | 84 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| completed | njs.cve-2021-46462 | A1 | `d8bf18d3` | $1.61 | 98 | 5 | 46 | 0 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2021-46462 | A2 | `c74a2268` | $0.94 | 85 | 4 | 44 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-27007 | A1 | `d8094420` | $2.21 | 119 | 9 | 78 | 12 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | njs.cve-2022-27007 | A2 | `3aa29160` | $1.28 | 88 | 4 | 43 | 0 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-28049 | A1 | `4f371824` | $0.78 | 71 | 1 | 47 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-28049 | A2 | `928ab779` | $1.19 | 50 | 1 | 28 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-29369 | A1 | `4b8eed06` | $2.63 | 156 | 8 | 88 | 10 | 0 | 32 | 8 | 23 | 1 | 1 | 0 | 1 | 0 |
| completed | njs.cve-2022-29369 | A2 | `41aad091` | $2.09 | 126 | 6 | 82 | 1 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-29779 | A1 | `a87179e0` | $1.63 | 115 | 6 | 67 | 3 | 0 | 15 | 4 | 11 | 0 | 1 | 0 | 1 | 0 |
| completed | njs.cve-2022-29779 | A2 | `a1d680c2` | $1.82 | 101 | 7 | 82 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-29780 | A1 | `591cd67f` | $0.87 | 71 | 1 | 31 | 0 | 0 | 13 | 4 | 8 | 1 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-29780 | A2 | `bf8a35d4` | $0.99 | 65 | 4 | 43 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-31306 | A1 | `3384d460` | $1.59 | 103 | 12 | 61 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | njs.cve-2022-31306 | A2 | `8f81b790` | $0.79 | 54 | 1 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-31307 | A1 | `a49d1630` | $1.37 | 102 | 2 | 61 | 3 | 0 | 17 | 4 | 11 | 2 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-32414 | A1 | `7a113952` | $1.27 | 101 | 5 | 62 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-34029 | A1 | `87de8aac` | $1.27 | 86 | 1 | 40 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-38890 | A1 | `1fe9d85d` | $1.75 | 130 | 3 | 74 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | njs.cve-2022-43284 | A1 | `6327bfa8` | $2.48 | 187 | 7 | 132 | 34 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| completed | openexr.cve-2020-16587 | A1 | `60035cb0` | $0.79 | 67 | 0 | 29 | 0 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | openexr.cve-2020-16587 | A2 | `266e43f0` | $1.04 | 91 | 1 | 58 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | openexr.cve-2020-16588 | A1 | `02f3264a` | $1.10 | 76 | 1 | 35 | 2 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 1 |
| completed | openexr.cve-2020-16588 | A2 | `08c8e4d2` | $2.17 | 121 | 12 | 63 | 12 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | openexr.cve-2020-16589 | A2 | `4c15757b` | $1.98 | 122 | 6 | 85 | 9 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | openjpeg.cve-2016-10507 | A1 | `f52347b5` | $1.06 | 87 | 1 | 46 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | openjpeg.cve-2016-10507 | A2 | `f690dfdb` | $0.76 | 57 | 1 | 33 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | openjpeg.cve-2016-7445 | A1 | `664785de` | $0.70 | 63 | 3 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | openjpeg.cve-2016-7445 | A2 | `4b5ee1df` | $0.62 | 51 | 1 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | openjpeg.cve-2017-14041 | A1 | `a73b768e` | $0.83 | 65 | 1 | 27 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 1 |
| completed | openjpeg.cve-2017-14041 | A2 | `ded9253b` | $0.67 | 50 | 1 | 22 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | openjpeg.cve-2021-3575 | A1 | `0244bb6a` | $1.73 | 107 | 8 | 72 | 12 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | openjpeg.cve-2021-3575 | A2 | `9ef59953` | $1.91 | 113 | 7 | 55 | 11 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | openjpeg.cve-2024-56827 | A1 | `87c9334e` | $1.76 | 108 | 8 | 65 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | openjpeg.cve-2024-56827 | A2 | `730a8f70` | $1.95 | 128 | 8 | 79 | 9 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | qpdf.cve-2021-36978 | A1 | `a332404d` | $1.64 | 97 | 3 | 46 | 0 | 0 | 17 | 4 | 12 | 1 | 1 | 1 | 1 | 1 |
| completed | qpdf.cve-2021-36978 | A2 | `fcf359d6` | $0.93 | 75 | 2 | 44 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 1 |
| completed | readstat.cve-2018-5698 | A1 | `4e51a6c6` | $1.75 | 131 | 8 | 89 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| completed | readstat.cve-2018-5698 | A2 | `171131fc` | $2.17 | 110 | 7 | 56 | 4 | 0 | 12 | 4 | 8 | 0 | 1 | 0 | 1 | 0 |
| completed | upx.cve-2017-15056 | A1 | `de3a100d` | $4.61 | 214 | 3 | 142 | 2 | 1 | 17 | 4 | 11 | 1 | 1 | 0 | 1 | 0 |
| completed | upx.cve-2017-15056 | A2 | `59fa0474` | $1.65 | 116 | 4 | 62 | 5 | 0 | 17 | 4 | 11 | 2 | 1 | 0 | 1 | 0 |
| completed | upx.cve-2020-27787 | A1 | `9c304c8e` | $2.52 | 128 | 5 | 61 | 5 | 0 | 17 | 4 | 12 | 1 | 1 | 0 | 1 | 0 |
| completed | upx.cve-2023-23457 | A1 | `04c1d9f6` | $2.30 | 111 | 4 | 64 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| completed | upx.cve-2023-23457 | A2 | `38a14cd3` | $2.73 | 125 | 7 | 83 | 20 | 0 | 15 | 4 | 11 | 0 | 1 | 0 | 1 | 0 |
| in_progress | njs.cve-2019-13617 | A2 | `a29682f5` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 |
| in_progress | njs.cve-2020-24348 | A1 | `11be3a4f` | n/a | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| timed_out / inactivity_timeout | exiv2.cve-2017-14859 | A1 | `a559b537` | $2.69 | 138 | 1 | 90 | 0 | 0 | 15 | 4 | 11 | 0 | 1 | 1 | 1 | 0 |
| timed_out / inactivity_timeout | exiv2.cve-2017-17723 | A1 | `562fcb90` | $3.52 | 171 | 1 | 106 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 0 |
| timed_out / inactivity_timeout | exiv2.cve-2018-19607 | A1 | `88df382f` | $1.51 | 94 | 1 | 57 | 1 | 0 | 14 | 4 | 9 | 1 | 1 | 1 | 1 | 0 |
| timed_out / inactivity_timeout | faad2.cve-2018-20357 | A1 | `506cf78b` | $1.88 | 141 | 6 | 81 | 1 | 0 | 17 | 4 | 11 | 2 | 1 | 0 | 1 | 0 |
| timed_out / inactivity_timeout | faad2.cve-2018-20361 | A1 | `3859ece7` | $2.56 | 112 | 5 | 65 | 0 | 0 | 16 | 4 | 11 | 1 | 1 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | faad2.cve-2018-20361 | A2 | `ebf6b095` | n/a | 69 | 3 | 44 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | faad2.cve-2021-32272 | A1 | `740c15a8` | n/a | 86 | 1 | 55 | 0 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 0 | 0 |
| timed_out / inactivity_timeout | libarchive.cve-2017-14503 | A1 | `2dfc6c20` | n/a | 20 | 1 | 8 | 0 | 0 | 8 | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | libarchive.cve-2017-14503 | A2 | `a03d773a` | $2.94 | 177 | 3 | 108 | 10 | 0 | 12 | 4 | 8 | 0 | 1 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | libiec61850.cve-2023-27772 | A1 | `1edf7943` | $2.10 | 138 | 1 | 94 | 2 | 0 | 12 | 4 | 8 | 0 | 1 | 1 | 1 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2021-28236 | A2 | `2c882ccf` | n/a | 73 | 1 | 55 | 2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2021-28237 | A1 | `a8a27fdb` | n/a | 18 | 1 | 6 | 0 | 0 | 8 | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2021-39521 | A2 | `3396ddec` | n/a | 49 | 1 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2021-42585 | A1 | `4db2843c` | n/a | 27 | 1 | 11 | 0 | 0 | 8 | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2021-42586 | A1 | `48faab36` | $0.20 | 17 | 0 | 10 | 0 | 0 | 5 | 4 | 1 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2021-42586 | A2 | `bf601850` | $5.44 | 206 | 5 | 161 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| timed_out / inactivity_timeout | libredwg.cve-2022-45332 | A1 | `824575f9` | n/a | 22 | 1 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | md4c.cve-2020-26148 | A2 | `59411179` | $1.15 | 90 | 3 | 58 | 7 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| timed_out / inactivity_timeout | md4c.cve-2021-30027 | A1 | `328c9476` | $1.45 | 96 | 6 | 62 | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| timed_out / inactivity_timeout | mruby.cve-2018-12247 | A1 | `d4b46983` | $0.89 | 66 | 1 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| timed_out / inactivity_timeout | mruby.cve-2022-0240 | A2 | `2a861e97` | $2.35 | 173 | 10 | 87 | 2 | 0 | 16 | 4 | 11 | 1 | 1 | 1 | 1 | 0 |
| timed_out / inactivity_timeout | njs.cve-2022-34029 | A2 | `9c8c91cd` | $0.68 | 71 | 5 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | openexr.cve-2020-16589 | A1 | `4a2850ee` | n/a | 3 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | php.cve-2017-12933 | A1 | `0a105a50` | n/a | 27 | 1 | 11 | 0 | 0 | 8 | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | php.cve-2018-12882 | A1 | `a0f23cb1` | n/a | 16 | 1 | 5 | 0 | 0 | 8 | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| timed_out / inactivity_timeout | upx.cve-2020-27787 | A2 | `6a4b49f9` | n/a | 100 | 2 | 76 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| failed / provider_failure | mruby.cve-2022-0623 | A1 | `b56be8e7` | $1.42 | 122 | 5 | 70 | 1 | 0 | 18 | 4 | 12 | 2 | 1 | 1 | 1 | 0 |
| failed / provider_failure | njs.cve-2022-31307 | A2 | `6649a27a` | $0.90 | 82 | 5 | 43 | 3 | 0 | 10 | 4 | 6 | 0 | 1 | 0 | 0 | 0 |
| failed / provider_failure | njs.cve-2022-32414 | A2 | `6b8780d2` | $0.95 | 36 | 8 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| failed / provider_failure | njs.cve-2022-38890 | A2 | `ee8ba8ab` | $0.45 | 43 | 1 | 25 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |
| failed / provider_failure | njs.cve-2022-43284 | A2 | `0865766b` | $0.12 | 11 | 1 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | faad2.cve-2021-32278 | A1 | `1d0cbbfa` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libarchive.cve-2019-11463 | A1 | `36accf4c` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libarchive.cve-2019-11463 | A2 | `cdbbe481` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libarchive.cve-2020-21674 | A1 | `eeeeca66` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | libarchive.cve-2020-21674 | A2 | `94495bc8` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | mruby.cve-2022-1427 | A1 | `78bfa9f1` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | mruby.cve-2022-1427 | A2 | `a186cc65` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | mruby.cve-2022-1934 | A1 | `654aa16c` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | mruby.cve-2022-1934 | A2 | `1a83b170` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | njs.cve-2023-27727 | A1 | `20f002ec` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | njs.cve-2023-27727 | A2 | `191e7aff` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | njs.cve-2023-27728 | A1 | `62057cd9` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | njs.cve-2023-27728 | A2 | `29b5f954` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | njs.cve-2023-27730 | A1 | `a59f1613` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| failed / work_error | njs.cve-2023-27730 | A2 | `48dfbca1` | $0.00 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |


## Generated Files

- `experiments/a12-batch-autogen/reports/a12_claude_code_cli_metrics.csv`
- `experiments/a12-batch-autogen/reports/a12_claude_code_cli_pairs.csv`
- `experiments/a12-batch-autogen/reports/a12_claude_code_cli_summary.json`
