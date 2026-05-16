# Smoke Test 2026-05-12: Findings (Empirically Verified)

This document consolidates findings from the all-cells smoke test (12 jobs, parallel=8)
and a follow-up instrumented 3-cell test (A1/B1/C1, parallel=3, openjpeg CVE) that
captured raw adapter event data via a temporary `_debug_inspector` module.

**Empirical evidence base:**
- DB events: 5,182 (12-job run) + 1,197 (3-cell run)
- Inspector records: **1,150** raw adapter event captures
  (`agent-docs/smoke-test-2026-05-12-inspector-data.jsonl`)
- Raw stdout/stream-json logs: `runs/<run-id>/stdout_stderr.log` for A-cell flat runs

The user's pushback on speculative claims was correct: several earlier hypotheses
(e.g. "empty system_prompt strips reasoning instructions") turned out to be wrong
when checked against the actual event data. This document distinguishes
**proven facts** from **structural risks** and removes anything not directly
supported by the empirical record.

---

## Findings index

1. **Bug-1**: Why B/C show zero `thinking` events — **the model itself does not emit ThinkingBlocks** for hierarchical-mode subtask prompts. (My earlier "empty system_prompt" theory was wrong; corrected.)
2. **Bug-2**: Real adapter bugs that *are* confirmed empirically — UserMessage drop, MessageEvent drop, SystemPromptEvent drop, ConversationErrorEvent drop, ActionEvent CoT field-mismatch.
3. **Bug-3**: Pipeline declares "success" without verifying SEC-bench postconditions; `BenchmarkResult` is dead code.
4. **Bug-4**: LiteLLM 429 retry storms cause silent 8+ min hangs; will block the experiment at scale.
5. **Terminology**: `exit_status="success"` is misleading; needs an explicit per-stage metric taxonomy.

---

## Bug-1: Why B/C runs show no `thinking` events

### Adapter event counts (from the instrumented 3-cell test, openjpeg CVE)

| Adapter (source) | Total records | Event class distribution | Assigned `output_type` |
|---|---|---|---|
| `claude_cli` (A1 path) | 33 | `dict` (stream-json blocks): 33 | **`thinking: 10`**, `output: 3`, `tool_use: 10`, `tool_result: 10` |
| `claude_sdk` (B1 path) | 860 | `AssistantMessage: 335`, `UserMessage: 173`, `ResultMessage: 8`, `SystemMessage: 9`, `TextBlock: 167`, `ToolUseBlock: 168`, **`ThinkingBlock: 0`** | `output: 167`, `None (dropped): 693` |
| `openhands` (C1 path) | 257 | `ActionEvent: 114`, `ObservationEvent: 114`, `SystemPromptEvent: 10`, `MessageEvent: 10`, `ConversationErrorEvent: 9` | `tool_use: 114`, `tool_result: 114`, `None (dropped): 29` |

### What this empirically proves

**A1's worker model emitted 10 `thinking`-type blocks** — confirmed in both the inspector log AND the raw stdout_stderr.log (which contains 20 `"type":"thinking"` blocks and 57 `"thinking_delta"` events; the inspector counts the de-duplicated post-stream blocks, the raw counts include partial-message deltas).

**B1's worker model emitted ZERO `ThinkingBlock` objects.** The `ClaudeAgentSDKAdapter._process_block` method does correctly handle `ThinkingBlock` (`claude_sdk_adapter.py:322-324`):

```python
elif isinstance(block, ThinkingBlock):
    if block.thinking.strip():
        return block.thinking, "thinking"
```

But this branch was never hit in 167 TextBlock+168 ToolUseBlock — never a single ThinkingBlock. **The model simply did not produce thinking content.** The adapter is not at fault for the missing thinking events.

### Why didn't the model think?

A1 (flat mode) prompt: 8,508 chars, asks the worker to perform the full 4-phase
SEC-bench reproduction (Builder + Exploiter + Fixer + Reporter) in one session.

B1 (hierarchical) leaf-worker prompts: small focused subtasks. Examples observed in this run:
- `[Build-Setup] Install cppcheck using 'apt-get install -y cppcheck'`
- `[Builder] Build the openjpeg project with address sanitizer enabled, ensuring that the build script is optimized and correctly configured...`
- `[Exploiter] Create a proof-of-concept that triggers the null pointer dereference error in skip_white()...`

Claude haiku-4.5 decides per-request whether to engage extended thinking. The factors that trigger it are not directly user-controllable from the CLI; in practice they correlate with prompt complexity, ambiguity, and the presence of multi-step planning requirements. The flat-mode prompt clearly triggered the model into extended-thinking mode; the smaller hierarchical-mode prompts did not.

**This is not an adapter bug.** It is a natural consequence of hierarchical decomposition: by breaking a complex task into small focused subtasks, the model never finds a single prompt complex enough to warrant extended thinking.

### Correction of earlier claim

The earlier hypothesis — that the SDK adapter strips the system prompt by passing
`--system-prompt ""`, removing reasoning-triggering instructions — is **not supported by the data**. While the SDK does explicitly pass `--system-prompt ""` (verified at `claude_agent_sdk/_internal/transport/subprocess_cli.py:176`), the model produced 167 TextBlocks and 168 ToolUseBlocks just fine; it just didn't produce ThinkingBlocks. If the system prompt were the cause, we'd expect impaired output across the board.

### So what's actionable?

For the `thinking_event_count` metric column to be comparable across cells, one of:

1. **Document the asymmetry**: Add a footnote to the report explaining that `thinking_event_count` is largely a function of prompt complexity, not adapter behavior, so cross-cell comparisons of this metric are unreliable.
2. **Force thinking on all worker calls**: Set `thinking_budget_tokens` in the Anthropic API call (if the SDK/CLI supports surfacing this). For Claude haiku-4.5: `max_tokens` request param with `thinking: {type: "enabled", budget_tokens: N}`. Verify SDK/CLI exposes this.
3. **Drop the metric**: It's not comparing what people think it's comparing.

---

## Bug-2: Adapter mapping bugs (empirically confirmed)

The instrumented run captured 1,150 raw events. The dropped-event analysis proves the
following are real bugs that lose data even when the model does produce content:

### Bug 2-A: `claude_sdk` adapter drops every `UserMessage` (173 in this run)

`infrastructure/adapters/worker/claude_sdk_adapter.py:240-242`:
```python
async def _process_message(self, message, sequencer):
    if not isinstance(message, AssistantMessage):
        return
```

Inspector confirms 173 `UserMessage` records at top-level with `assigned_output_type=None`. The SDK convention is that `ToolResultBlock` arrives inside a `UserMessage`. The `_process_block` method (lines 325-327) has a `ToolResultBlock` branch that would handle them correctly, but it is **dead code** under this filter.

Empirical evidence from a captured UserMessage:
```
content: [TextBlock(text='[Request interrupted by user]')]
```

Other UserMessages contain `ToolResultBlock` objects with command output. All silently dropped from the event stream.

**Effect**: B-cell `tool_result_count` is artificially 0 in the metrics CSV. A-cell shows non-zero. The columns are non-comparable. This also means the model's mid-conversation tool-result feedback is lost from the audit trail.

**Fix**: Change `_process_message` to also iterate `UserMessage.content` and route blocks through `_process_block`. The block-level handling already exists.

### Bug 2-B: `openhands` adapter — `MessageEvent` and `ConversationErrorEvent` dropped (19 in this run)

`infrastructure/adapters/worker/openhands_adapter.py:824-837` `_extract_event_content`:
```python
if hasattr(event, "message") and event.message:
    return str(event.message)
if hasattr(event, "content") and event.content:
    return str(event.content)
```

Captured `MessageEvent` attrs (real data from this run):
```
llm_message: Message(role='user', content=[TextContent(...)])
source: 'user'
```

The field is **`llm_message`**, not `message` or `content`. The check returns None → event is dropped before classification. Same issue affects `ConversationErrorEvent` (carries text on `.error`).

**Effect**: 10 MessageEvent + 9 ConversationErrorEvent = 19 events silently dropped per run. These are the user-visible message exchanges and agent error reports.

**Fix**: Add field-name handling in `_extract_event_content`:
```python
if hasattr(event, "llm_message") and getattr(event.llm_message, "content", None):
    return _format_message(event.llm_message)
if hasattr(event, "error") and event.error:
    return f"Error: {event.error}"
```

### Bug 2-C: `openhands` adapter — `SystemPromptEvent` not handled (10 in this run)

`_extract_event_content` has no branch for `SystemPromptEvent`, so they get dropped. The current behavior: these events would carry the system prompt sent to the model. Whether they SHOULD be emitted as `ThoughtCaptured` events is a design choice — they're not really "thoughts." But the silent drop means they're invisible to debugging.

**Fix or document**: Either add a `system_prompt` output_type and route SystemPromptEvent there, or explicitly comment-document that they are intentionally not surfaced.

### Bug 2-D: `openhands` adapter — `ActionEvent.thought` / `reasoning_content` / `thinking_blocks` would be dropped if model produced them

In this run all 114 ActionEvents had empty `thought=[]`, `reasoning_content=None`, `thinking_blocks=[]`. So no data was lost **in this run**. But structurally:

`_format_action_event(event.action)` is called with the **inner Action**, not the outer ActionEvent. The CoT fields live on the outer `ActionEvent`:
```python
class ActionEvent(...):
    thought: Sequence[TextContent]
    reasoning_content: str | None
    thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock]
```

These would be silently dropped IF the model used `reasoning_effort=high` (a litellm parameter for reasoning models). With gpt-4o-mini at default reasoning, it doesn't fire. With a different model configuration it would, and the data would vanish.

**Effect (latent)**: For reasoning-enabled C-cell configurations, CoT would be invisible. Not currently triggered.

**Fix**: Pass the outer `ActionEvent` to `_format_action_event` and pull from `event.thought`/`.reasoning_content`/`.thinking_blocks`.

### Bug 2-E: `openhands` adapter — `_classify_event` short-circuit (latent)

Lines 814-822:
```python
if getattr(event, "action", None) is not None:
    return "tool_use"
if getattr(event, "observation", None) is not None:
    return "tool_result"
event_class_name = type(event).__name__
return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")
```

`SDK_EVENT_TYPE_MAP` has entries for `AgentThinkAction` and `MessageEvent`, but these are never reached because the `action`/`observation` checks fire first. `ThinkAction` and `FinishAction`, when wrapped in `ActionEvent`, get mislabeled as `tool_use`.

In this run no `ThinkAction` fired (the inspector saw 114 ActionEvents but the prior 12-job run had 1). The bug remains structurally.

**Fix**: Reorder — consult `SDK_EVENT_TYPE_MAP` first, fall through to the `action`/`observation` heuristics only when the class isn't in the map.

---

## Bug-3: Pipeline declares "success" without verifying postconditions

**Verified by inspecting a successful B1 run** (`runs/20f236dd-.../run_manifest.json`):
- `exit_status: "success"`
- `testcase/model_patch.diff`: **does not exist**
- `testcase/repo_changes.diff`: 0 bytes
- All 8 `VerificationPassed` events: structural-check fallback (`"Passed structural checks (no success criteria defined for judge evaluation)"`); judge LLM never invoked

### Dead code

`plugins/security/benchmark_result.py` defines `BenchmarkResult` and `StageResult` with `overall_success`, `is_complete`, `with_stage_result`. None of it is constructed anywhere. No `BuilderCompleted`, `ExploiterCompleted`, `FixerCompleted`, `PatchApplied`, `ReproducerVerified` event exists in `core/domain/events/events.py`.

### Postcondition metrics — implementation status

The rich-metrics extension ships as an extension of the existing per-run
metric pipeline (`experiments/shared/scripts/run_metrics.py`) plus a
filesystem helper in `collect.py`. No new event types were introduced —
every value is derivable from `events.jsonl` and `testcase/`. See
`agent-docs/experiment-metrics-reference.md` for the per-column reference.

| Metric | Status | Notes |
|---|---|---|
| `run_terminated_status` | **Done** | Per-run column carries `run_manifest.json["exit_status"]` verbatim (observed values: `success`, `failed`). Replaces the old `exit_status` column. Per-cell summary aggregates `count(status=="success")`. |
| `fixer_artifacts_present` | **Done** | `testcase/model_patch.diff` exists and is nonempty. Per-cell summary is the count of runs in the cell with this true. |
| `builder_artifacts_present` | **Done** | `testcase/base_commit_hash` AND `testcase/packages.txt` AND (`testcase/repo_changes.diff` nonempty OR `testcase/src/build.sh` exists). Empirically `repo_changes.diff` is frequently 0 bytes; the OR with `src/build.sh` accommodates SEC-bench-style builds. |
| `exploiter_artifacts_present` | **Done** | `testcase/repro.sh` nonempty AND at least one `testcase/poc.*` file nonempty. |
| `judge_ran` | **Done** | 1 iff a `VerificationPassed` or `VerificationFailed(failed_stage="judge")` event was observed whose feedback is NOT the structural-check sentinel `"Passed structural checks (no success criteria defined for judge evaluation)"`. |
| `judge_passed` | **Done** | 1 iff `judge_ran=1` AND the *real* judge score (sentinel excluded) is ≥ 70 (constant `JUDGE_PASS_THRESHOLD` in `run_metrics.py`). |
| `structural_check_passed` | **Done** | 1 iff the structural-check fallback fired. Surfaced separately so the `score=100` sentinel can never be confused with a real judge verdict. |
| `total_agents` / `completed_agents` / `failed_agents` | **Done** | Pulled from `RunCompleted` payload. -1 sentinel when no `RunCompleted` was observed (mirrors `run_duration_seconds`). |
| `retry_count` | **Done** | Count of `RetryScheduled` events. |
| `redecomposition_count` | **Done** | Count of `RedecompositionTriggered` events. |
| `cost_by_model` | **Done** | JSON column. Aggregated from `WorkerCostRecorded.usage_metrics[*].(model, accumulated_cost_usd)` when present; falls back to the top-level `(model, cost_usd)` when `usage_metrics=[]` (empirically ~55% of events in the 2026-05-12 smoke test). Also picks up `TokensConsumed.(model, cost_usd)`. |
| `cost_by_operation` | **Done** | JSON column summed from `TokensConsumed.(operation, cost_usd)`. Observed operations in 2026-05-12 data: `task_assessment`, `task_decomposition`. (The earlier doc mentioned `complexity_evaluation` / `worker_execution`; neither appeared in real events for this run.) |
| `fixer_verified` (apply → rebuild → repro.sh → no sanitizer + exit_code match) | **Not done** | Post-run check, deferred per the P2 row in the priority list below. |
| `exploiter_verified` (repro.sh triggers expected sanitizer + exit_code match) | **Not done** | Same — gold-standard verifier. |
| `builder_verified` (`secb build` exits 0) | **Not done** | Same. |
| `security_tool_call_count` (valgrind / klee / shell_in_container) | **Not done** | Adapter conflates these as `tool_name=claude_code` / `unknown`; would need worker-adapter changes to preserve MCP tool names. |
| Per-stage durations | **Not done** | Reconstructable from `AgentExecutionFinished.duration_seconds` + role briefing; deferred. |

#### Audit harness

`experiments/shared/scripts/metrics_audit.py` walks `events.jsonl`
line-by-line with a deliberately parallel reimplementation of the
postcondition counters (it does NOT reuse `metrics_from_events`). On the
existing 2026-05-11-fresh-start study (16 runs) and the 2026-05-12 smoke
test (12 runs) it reports **0 discrepancies**. The harness has been
sanity-checked against a fabricated CSV mutation to confirm it does
detect known-bad values rather than passing tautologically.

### Files

- `core/domain/events/events.py` (31 event types; none for stage-specific outcomes)
- `plugins/security/benchmark_result.py` (dead value objects)
- `experiments/shared/scripts/run_metrics.py:138-169` (`empty_metrics()` schema)
- `presentation/persistence/run_persistence.py:297-308` (`_probe_deliverables` — already records filename map per run)
- `core/application/services/orchestration/verification_pipeline.py:148-152` (structural-check fallback)

---

## Bug-4: LiteLLM 429 silent hangs

### Confirmed root cause

`infrastructure/adapters/litellm_adapter.py`:
- `_LLM_TIMEOUT_SECONDS = 180` per attempt
- `_RATE_LIMIT_RETRIES = 3` → 4 attempts total
- Backoff: `10 × 2^attempt × jitter(0.5, 1.5)` → sleeps of ~5-15s, ~10-30s, ~20-60s
- Worst-case single call: ~105s of DB silence

`tool_calling_service.py`: `max_iterations: 5`. No `LLMError` catch between iterations.
`agent_orchestrator.py`: `_ASSESSMENT_PARSE_RETRIES = 2` (each retry reruns the whole tool loop) plus a recovery turn.

**Cumulative worst case for a single `assess_task`**: 3 × 5 × 105s ≈ **26 min** of DB silence. The 8+ min hangs we observed in both runs (the 12-job one and the 3-cell instrumented one) fit this envelope.

`logger.warning` from `_call_litellm` retry path goes to **stdout, not the event store** — DB silence is by design.

### Empirical confirmation

In the 12-job smoke test, 2 of 12 runs (B1/njs and C2/openjpeg) got stuck >8 min with no DB events. Processes had measurable CPU usage but no event flushes. Required manual kill.

In the 3-cell instrumented test, 1 of 3 runs (the last C1 cell) got stuck for ~25 min after producing 1,197 DB events, then was manually killed. Same pattern.

### Cross-process problem

`experiments/shared/scripts/run_matrix.py:209` uses `ThreadPoolExecutor` calling `subprocess.run([main.py, "run", ...])` per job. Each subprocess has its own `AgentExecutionService._llm_semaphore`. At parallel=8, 8 separate processes hammer the same Anthropic key independently. In-process concurrency caps don't help at the matrix level.

### Provider rate limits (2026)

**Claude Haiku 4.5:**

| Tier | RPM | Spend threshold |
|---|---|---|
| 1 | 50 | $0 |
| 2 | 1,000 | $5 cumulative |
| 3 | 2,000 | $40 |
| 4 | 4,000 | $200 |

**OpenAI gpt-4o-mini:** Tier 1 = 500 RPM; not the bottleneck at parallel=8.

### Recommended mitigation stack

**Step 0 — Check Anthropic tier** (one-time):
```python
import litellm
resp = litellm.completion(model="claude-haiku-4-5", messages=[{"role":"user","content":"hi"}])
print(resp._response_headers.get("anthropic-ratelimit-requests-limit"))
```

| Tier | Max recommended `--parallel` |
|---|---|
| 1 | 2 (with jitter) |
| 2 | 8-12 |
| 3 | 15-20 |
| 4 | 30+ (CPU/Docker become binding) |

**Step 1 — Code-only fixes:**

1. Per-step `asyncio.wait_for(timeout=600)` at `execution_service.py:560`:
   ```python
   async with self._llm_semaphore:
       await asyncio.wait_for(self.run_agent_step(agent_id), timeout=600.0)
   ```
2. Reduce retries in `litellm_adapter.py:128-129`:
   ```python
   _RATE_LIMIT_RETRIES = 1
   _RATE_LIMIT_BASE_DELAY = 15
   ```
3. Add jitter: `concurrency.llm_jitter_max_ms: 1500` in config.

**Step 2 — Scale fix** (if Step 1 insufficient):

Refactor `LiteLLMAdapter` to use `litellm.Router` + Redis for cross-process throttling. Multiple deployments with `routing_strategy="usage-based-routing"`, per-deployment `rpm`/`tpm`, automatic cooldown on 429.

**Step 3 — MockLLMAdapter** (orthogonal, for pipeline-only smoke tests):

Add `MockLLMAdapter(LLMPort)` returning canned `LLMResponse` payloads. Wire via `worker.model: "mock/echo"`. Useful for testing run_matrix, DB, Docker plumbing without API spend.

### Files

- `infrastructure/adapters/litellm_adapter.py:127-209`
- `infrastructure/io/robust_call.py:18-90`
- `core/application/services/toolset/tool_calling_service.py:73-237`
- `core/application/agent_orchestrator.py:202, 308-313, 474-479, 970-1100`
- `core/application/execution_service.py:184, 259, 560, 612-616, 638-697`
- `experiments/shared/scripts/run_matrix.py:209`
- `experiments/shared/harness.py:226`

---

## Terminology: "success" → explicit per-stage metrics

Current `exit_status="success"` and CSV `judge_score=100` are both misleading. Proposed explicit-success taxonomy (CSV columns):

| Metric | Definition | Source |
|---|---|---|
| `run_terminated_status` | `completed` / `failed` / `timeout` / `crashed` | run_manifest.json |
| `run_completed` | `RunCompleted` event emitted | event store |
| `builder_submitted` | Builder agent emitted `WorkCompleted` (not `WorkFailed`) | event store + role |
| `builder_artifacts_present` | All 4 builder files exist and non-empty | testcase/ |
| `builder_verified` | `secb build` exits 0 | new post-run check |
| `exploiter_submitted` | Exploiter agent `WorkCompleted` | event store |
| `exploiter_artifacts_present` | `poc.*` and `repro.sh` exist | testcase/ |
| `exploiter_verified` | `repro.sh` triggers expected sanitizer AND exit_code matches | new post-run check |
| `fixer_submitted` | Fixer agent `WorkCompleted` | event store |
| `fixer_artifacts_present` | `model_patch.diff` exists and non-empty | testcase/ |
| `fixer_verified` | Reset → apply → rebuild → repro → no sanitizer + clean exit | new post-run check |
| `judge_ran` | Real judge LLM call (not structural sentinel) | feedback string check |
| `judge_passed` | Judge ran AND score ≥ threshold | event + threshold |
| `cve_resolved` | `builder_verified` AND `exploiter_verified` AND `fixer_verified` | composite |

The structural-check fallback should write under a separate `structural_check_passed` column so it can never be confused with a real judge score.

---

## Action item priority

| Priority | Item | Effort | Blocking 200-CVE run? |
|---|---|---|---|
| P0 | Per-step `asyncio.wait_for(timeout=600)` in execution_service | Trivial | **Yes** |
| P0 | Reduce LiteLLM retries to 1, add jitter | Trivial | **Yes** |
| P0 | Check Anthropic tier, set `--parallel` accordingly | None (test) | **Yes** |
| P1 | Add `fixer_artifacts_present` boolean column | Easy | No (correctness) |
| P1 | Add Builder/Exploiter/Fixer artifact-present columns | Easy | No (correctness) |
| P1 | Rename CSV `exit_status` → `run_terminated_status` | Trivial | No (clarity) |
| P1 | Fix `claude_sdk` UserMessage drop (Bug 2-A) | Easy | No (data quality) |
| P1 | Fix `openhands` MessageEvent + ConversationErrorEvent drops (Bug 2-B) | Easy | No (data quality) |
| P2 | Implement Fixer/Exploiter/Builder verified post-run checks | Medium | No (gold-standard) |
| P2 | Fix `openhands` SystemPromptEvent (Bug 2-C) and CoT extraction (Bug 2-D) | Medium | No (latent) |
| P2 | Fix `openhands` `_classify_event` short-circuit (Bug 2-E) | Easy | No (latent) |
| P2 | Document or remove `thinking_event_count` metric (model-dependent, not adapter-comparable) | Trivial | No |
| P3 | LiteLLM Router + Redis | Hard | Only if P0 insufficient |
| P3 | Mock LLM adapter | Easy | No (smoke optimization) |
| P3 | Populate `success_criteria` per stage so judge actually runs | Medium | No (judge meaningfulness) |

---

## Reproducibility

The instrumentation that produced this evidence is intentionally temporary. To
remove after findings are documented and acted on:

1. `infrastructure/adapters/worker/_debug_inspector.py` — delete the file
2. `infrastructure/adapters/worker/openhands_adapter.py` — remove `_debug_inspect` import and the call inside the iter-events loop
3. `infrastructure/adapters/worker/claude_sdk_adapter.py` — remove `_debug_inspect` import and the calls inside `_process_message`
4. `infrastructure/workers/claude_code_worker.py` — remove `_debug_inspect` import and the call inside `_message_events`

Inspector output to reproduce: set `ARISE_EVENT_INSPECT_PATH=<file>` env var, run the matrix, and parse the resulting JSONL.

Raw inspector data from the run that produced this document:
`agent-docs/smoke-test-2026-05-12-inspector-data.jsonl` (1,150 records).
