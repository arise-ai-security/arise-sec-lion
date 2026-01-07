# Solutions Branch Implementation Guide

Based on the SEC-bench experiment failure analysis, this document outlines solutions to implement in a new branch. Solutions are prioritized by data backing and expected impact.

---

## Priority 1: Data-Backed Solutions (Implement First)

### S1. Dynamic `secb repro` Generation (Addresses H1)

**Problem:** PoC filename mismatch causes false negatives. SEC-bench's `secb repro()` expects hardcoded filenames (e.g., `/testcase/input.zip`), but workers create different filenames (e.g., `/testcase/arise_poc.zip`).

**Evidence:**
- 14/23 Exploiter failures linked to filename mismatch
- `exit_code: 127` (file not found) in verification logs
- Counter-evidence: 4 non-exiv2 projects succeeded, indicating mismatch isn't universal

**Solution:** Modify the Exploiter phase prompt to:
1. Generate a custom `secb.sh` script that references the actual created filenames
2. OR modify workers to create files with SEC-bench's expected names
3. Include explicit instructions about expected filenames in the task context

**Implementation:**
```yaml
# In exploiter prompt template, add:
- "CRITICAL: The verification harness expects specific filenames. Check the
  instance's secb_sh field for expected paths (e.g., /testcase/input.zip).
  Either create files with these exact names OR generate a custom secb.sh
  that references your actual output files."
```

**Config Changes:** None required (prompt-level change)

**Validation Metric:** Exploiter success rate improvement (baseline: 39.1%)

---

### S2. Increase Depth Limit (Addresses H2)

**Problem:** `max_depth=4` causes premature task assignment. Workers at depth limit receive vague, analysis-oriented tasks instead of concrete actions.

**Evidence:**
- 49.5% of depth-limited workers receive "analyze/investigate" tasks vs 26.2% for natural workers
- Runs with ≥30% depth-limited workers: 41% overall success
- Runs with 0% depth-limited workers: 78% overall success

**Solution:** Increase `max_depth` to allow deeper decomposition.

**Implementation:**
```yaml
# config/config.yaml
orchestration:
  limits:
    max_depth: 8  # Increased from 4
    # Alternative: max_depth: -1 (infinite, with monitoring)
```

**Staged Rollout:**
1. **Phase 1:** Set `max_depth: 8` and run 5 instances
2. **Phase 2:** If worker explosion occurs (>100 agents), reduce to 6
3. **Phase 3:** If stable, consider `max_depth: -1` with `max_total_agents: 100` as safety net

**Validation Metric:**
- Reduction in depth-limited worker percentage
- Improvement in task specificity (action verbs %, specific file paths %)

---

### S3. Worker Timeout Optimization (Addresses Cost/Time)

**Problem:** Workers retry unproductive loops, inflating cost ($8.96/instance vs $0.87 baseline).

**Evidence:**
- `libarchive.cve-2019-11463`: 5.4h runtime, $28.54 cost (14% of total budget), still failed
- Builder phase shows "repeated failure-retry cycles" in logs

**Solution:** Implement tiered timeout strategy with early termination.

**Implementation:**
```yaml
# config/config.yaml
orchestration:
  worker_timeout: 600.0        # Increased from 300.0 for legitimate long tasks
  worker_idle_timeout: 120.0   # NEW: Terminate if no progress for 2 min
  max_worker_retries: 2        # NEW: Reduce from implicit unlimited retries

worker:
  timeout: 180                 # Increased from 120 for complex tasks
  max_consecutive_failures: 3  # NEW: Stop after 3 consecutive failures
```

**Additional Logic (Code Change):**
```python
# In worker execution step, add early termination check:
if consecutive_no_progress_iterations > 3:
    return WorkerResult(
        status="terminated",
        reason="No progress detected - possible infinite loop"
    )
```

**Validation Metric:**
- Reduction in average cost per instance
- Reduction in outlier runtimes (>3h)

---

### S4. Increase LLM Max Tokens (Addresses H3)

**Problem:** `gpt-4o-mini` produces malformed JSON due to token truncation.

**Evidence:**
- 831 parse-related events in logs
- `WorkFailed` events show "LLM violated limits" errors
- Cascade failures when parent agents fail to parse

**Solution:** Increase `max_tokens` for orchestration models.

**Implementation:**
```yaml
# config/config.yaml
boss:
  max_tokens: 8000   # Increased from 4000

manager:
  max_tokens: 8000   # Increased from 4000
```

**Additional Safeguards:**
```python
# Add JSON validation with retry in LLM response handler:
def parse_llm_response(response: str, max_retries: int = 2) -> dict:
    for attempt in range(max_retries + 1):
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            if attempt < max_retries:
                # Request LLM to fix its JSON
                response = retry_with_correction_prompt(response)
            else:
                raise
```

**Validation Metric:** Reduction in parse-related failures

---

## Priority 2: Partially Data-Backed (Implement After Priority 1)

### S5. Manager Tool-Calling Capability (Addresses H2 Root Cause)

**Problem:** Managers decompose tasks without context, creating vague subtasks. Workers waste cycles on "analysis" that managers should handle.

**Evidence:**
- 49.5% vague tasks at depth limit contain "analyze/investigate" verbs
- Only 30.5% of depth-limited tasks reference specific file paths
- Natural workers: 70.5% specific file paths

**Solution:** Enable lightweight tool-calling for Managers (read-only operations).

**Implementation:**
1. **Manager Tools (Read-Only):**
   - `read_file`: Read file contents for context
   - `list_directory`: Understand project structure
   - `search_code`: Find relevant code sections
   - `check_file_exists`: Validate paths before assigning

2. **Worker Restrictions:**
   - Workers focus on code execution only
   - No "analyze" or "investigate" tasks assigned to workers

**Prompt Engineering:**
```markdown
# Manager System Prompt Addition:
Before decomposing tasks, use your tools to:
1. Verify file paths exist
2. Understand code structure
3. Identify concrete entry points

Workers should ONLY receive tasks that:
- Start with action verbs: create, implement, fix, add, write, build, run
- Reference specific file paths (e.g., /src/build.sh)
- Have measurable completion criteria

NEVER assign workers tasks containing: analyze, investigate, identify, evaluate, document
```

**Validation Metric:**
- Action verb % in worker tasks (target: >90%)
- Specific file path % in worker tasks (target: >60%)

---

## Priority 3: Optional Enhancements

### S6. Iterative Prompt Engineering

**Dependencies:** Implement after S1-S5 to isolate prompt impact from structural changes.

**Areas for Improvement:**

| Component | Current Issue | Prompt Enhancement |
|-----------|---------------|-------------------|
| Boss | Over-decomposition | Add "prefer fewer, well-defined phases" |
| Manager | Vague subtasks | Add concrete examples of good vs bad tasks |
| Worker | Filename mismatch | Add SEC-bench filename requirements |
| All | JSON formatting | Add explicit JSON schema in system prompt |

**A/B Testing Framework:**
```python
# Run same instances with prompt variants
prompt_variants = {
    "baseline": current_prompts,
    "concise": prompts_with_brevity_instructions,
    "structured": prompts_with_explicit_json_schema,
}
```

---

## Implementation Checklist
Organized from most simple fixes to most complex fixes.

### Branch Setup
```bash
git checkout -b solutions/secbench-fixes
```

### Phase 1: Quick Wins
- [ ] S4: Increase `max_tokens` to 8000
- [ ] S2: Set `max_depth: 8`
- [ ] S3: Add `max_worker_retries: 2`

### Phase 2: Prompt Changes
- [ ] S1: Update Exploiter prompt with filename requirements
- [ ] S5 (partial): Add task quality guidelines to Manager prompt

### Phase 3: Code Changes
- [ ] S3: Implement worker idle timeout detection
- [ ] S4: Add JSON validation with retry logic
- [ ] S5: Implement Manager tool-calling (if scope permits)

### Validation Run
```bash
# Run subset of failed instances
INSTANCES="libarchive.cve-2019-11463,upx.cve-2023-23457,yara.cve-2017-5924"
# Compare against baseline results
```

---

## Success Criteria

| Metric | Baseline | Target | Notes |
|--------|----------|--------|-------|
| Builder | 87.0% | >90% | Minor improvement expected |
| Exploiter | 39.1% | >55% | S1 should directly improve |
| Fixer | 26.1% | >40% | Dependent on Exploiter improvement |
| E2E | 26.1% | >40% | Compound effect |
| Avg Cost | $8.96 | <$5.00 | S3 should reduce outliers |
| Avg Duration | 1.5h | <1h | S3 timeout changes |

---

## Monitoring & Rollback

### Key Metrics to Track
1. Parse failure rate (should decrease with S4)
2. Depth-limited worker % (should decrease with S2)
3. Worker retry count (should decrease with S3)
4. Filename match rate in Exploiter (should increase with S1)

### Rollback Triggers
- Worker explosion: >100 agents in single run → reduce `max_depth`
- Cost explosion: >$20/instance → tighten timeouts
- Success rate regression: <20% E2E → revert to baseline

---

## Appendix: Configuration Diff

```diff
# config/config.yaml changes

boss:
-  max_tokens: 4000
+  max_tokens: 8000

manager:
-  max_tokens: 4000
+  max_tokens: 8000

orchestration:
-  worker_timeout: 300.0
+  worker_timeout: 600.0
+  worker_idle_timeout: 120.0
+  max_worker_retries: 2

  limits:
-    max_depth: 4
+    max_depth: 8
```
