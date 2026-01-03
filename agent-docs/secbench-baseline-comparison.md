# SEC-bench Baseline Comparison Analysis

This document validates your understanding of SEC-bench (SB) and the Arise Sec Lion (ASL) system, provides architectural flow diagrams, and outlines the experimental methodology for comparing performance.

---

## 1. Validation of Your Understanding

### 1.1 SEC-bench (SB) Architecture - MOSTLY CORRECT ✓

Your understanding is largely accurate with some clarifications:

| Your Statement | Validation | Notes |
|---|---|---|
| Preprocessor cleans Dockerfile from OSS-Fuzz | ✓ Correct | Prepares Docker environments from CVE reports |
| Builder prepares executable environment | ✓ Correct | Builds vulnerable code with sanitizers enabled |
| Exploiter writes `secb repro()` and PoC | ⚠️ Partially | SB uses `repro.sh` script, not a `repro()` function. Your system uses `secb_sh` from dataset instead |
| Fixer generates minimal patch | ✓ Correct | Creates diff that fixes vulnerability |
| Pipeline: Builder → Exploiter → Fixer | ✓ Correct | Sequential 3-phase pipeline |

### 1.2 The `repro` Function Clarification

**What `repro.sh` actually does:**
- It's a **shell script** (not a function) that contains the exact command to trigger the vulnerability
- Format: `#!/bin/bash` followed by commands like `./vulnerable_binary < /testcase/poc_input`
- **Purpose**: Provides a repeatable way to confirm the vulnerability is triggered by running the PoC against the built binary

**Why SB writes `repro.sh`:**
- The Exploiter agent must **discover** the correct invocation (binary path, arguments, input file)
- Writing `repro.sh` proves the agent understood how to trigger the vulnerability
- It enables automated validation: run `repro.sh` → check for sanitizer error → success/fail

### 1.3 Success/Failure Meanings

| Phase | Success Means | Failure Means |
|-------|---------------|---------------|
| **Builder** | Repository compiles with sanitizers; executable is produced | Build system is broken, dependencies missing, or incompatible compiler flags |
| **Exploiter** | PoC triggers **exact same sanitizer error** as bug report | Instance may not be a real bug, or PoC crafting failed, or wrong binary/arguments |
| **Fixer** | Patch applied, builds, and `repro.sh` **no longer triggers sanitizer error** | Root cause not understood, patch breaks functionality, or patch doesn't fix the issue |

### 1.4 Your Concern: "AI might delete vulnerable code"

This is a valid concern. SEC-bench addresses this by:
1. Requiring the patch to **not break functionality** (code should still work)
2. Using **minimal patch criteria** - reviewers check patch quality
3. Running the patched code to ensure it still executes correctly

In practice, deleting the vulnerable function would likely cause compilation errors or runtime failures in the test suite.

---

## 2. Flow Diagrams

### 2.1 SEC-bench (Original) Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            SEC-bench Pipeline                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐                                                           │
│  │ CVE Instance │ JSON file from HuggingFace dataset                        │
│  │   (Input)    │ • instance_id, repo, base_commit                          │
│  └──────┬───────┘ • bug_description, sanitizer_report                       │
│         │         • dockerfile, build_sh                                    │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                      PREPROCESSOR                                     │  │
│  │  • Cleans OSS-Fuzz Dockerfile                                        │  │
│  │  • Replaces OSS-Fuzz variables with global ones                      │  │
│  │  • Installs dependencies                                              │  │
│  │  • Creates Docker image: hwiwonlee/secb.eval.x86_64.{project}.{cve}  │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                         BUILDER (Single Agent)                        │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Responsibility:                                                      │  │
│  │  • Checkout to vulnerable commit (base_commit)                        │  │
│  │  • Run/fix build.sh to compile with sanitizers                        │  │
│  │  • Produce working executables in /src/{project}                      │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Deliverables:                                                        │  │
│  │  ✓ /testcase/base_commit_hash                                         │  │
│  │  ✓ /src/build.sh (improved, standalone)                               │  │
│  │  ○ /testcase/repo_changes.diff (if source modified)                   │  │
│  │  ○ /testcase/packages.txt (if packages installed)                     │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Success: Executable binary exists, sanitizers enabled                │  │
│  │  Failure: Cannot build → instance unusable for benchmark              │  │
│  └─────────────────────────────┬────────────────────────────────────────┘  │
│                                │                                            │
│                                ▼                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                       EXPLOITER (Single Agent)                        │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Responsibility:                                                      │  │
│  │  • Extract/create PoC from bug description                            │  │
│  │  • Write repro.sh that triggers the vulnerability                     │  │
│  │  • Verify sanitizer output matches expected error                     │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Deliverables:                                                        │  │
│  │  ✓ /testcase/repro.sh (working exploit command)                       │  │
│  │  ✓ PoC artifacts in /testcase (input files, scripts)                  │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Success: repro.sh triggers EXACT sanitizer error from bug report    │  │
│  │  Failure: Cannot reproduce → instance may be false positive           │  │
│  └─────────────────────────────┬────────────────────────────────────────┘  │
│                                │                                            │
│                                ▼                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                         FIXER (Single Agent)                          │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Responsibility:                                                      │  │
│  │  • Analyze root cause of vulnerability                                │  │
│  │  • Review candidate fix commits                                       │  │
│  │  • Generate minimal patch                                             │  │
│  │  • Validate: apply patch, rebuild, run repro.sh → no sanitizer error │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Deliverables:                                                        │  │
│  │  ✓ /testcase/model_patch.diff (unified diff format)                   │  │
│  ├──────────────────────────────────────────────────────────────────────┤  │
│  │  Success: Patch fixes vulnerability without breaking functionality   │  │
│  │  Failure: Root cause not understood, or patch is incorrect            │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

                           Success Rate Calculation
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  Builder SR = (Successful Builds) / (Total Instances)                       │
│  Exploiter SR = (Successful PoCs) / (Successful Builds)                     │
│  Fixer SR = (Successful Patches) / (Successful PoCs)                        │
│                                                                             │
│  End-to-End SR = Builder SR × Exploiter SR × Fixer SR                       │
│               OR = (Instances where ALL 3 succeed) / (Total Instances)      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Arise Sec Lion (ASL) Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Arise Sec Lion Pipeline                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐                                                           │
│  │ CVE Instance │ Same JSON format as SEC-bench                             │
│  │   (Input)    │ Uses pre-built Docker: hwiwonlee/secb.eval.x86_64.*      │
│  └──────┬───────┘ Uses secb_sh directly from dataset (NOT generated)       │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                              BOSS AGENT                               │  │
│  │           (Root of Agent Tree - Decomposes into 3 phases)             │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│         │                                                                   │
│         ├─────────────────────────────────────────────────────┐            │
│         │                                                     │            │
│         ▼                                                     │            │
│  ┌────────────────────┐                                       │            │
│  │ [Builder] MANAGER  │                                       │            │
│  │ (Depth 1)          │                                       │            │
│  └─────────┬──────────┘                                       │            │
│            │                                                  │            │
│            ├──────────────────────────────────┐               │            │
│            │                                  │               │            │
│            ▼                                  ▼               │            │
│  ┌──────────────────┐              ┌──────────────────┐       │            │
│  │ [Builder-1]      │              │ [Builder-5]      │       │            │
│  │ WORKER           │  ···         │ WORKER           │       │            │
│  │ Identify commit  │              │ Record changes   │       │            │
│  └──────────────────┘              └──────────────────┘       │            │
│                                                               │            │
│  ┌───────────────────────────────────────────────────────────┐│            │
│  │ Builder Phase Aggregate Result (5 workers)                ││            │
│  │ • All workers succeed → Builder SUCCESS                   ││            │
│  │ • Any worker fails → Builder FAIL                         ││            │
│  └───────────────────────────────────────────────────────────┘│            │
│                                                               │            │
│  ┌────────────────────────────────────────────────────────────┘            │
│  │                                                                          │
│  ▼                                                                          │
│  ┌────────────────────┐                                                     │
│  │ [Exploiter] MANAGER│                                                     │
│  │ (Depth 1)          │                                                     │
│  └─────────┬──────────┘                                                     │
│            │                                                                │
│            ├──────────────────────────────────┐                             │
│            │                                  │                             │
│            ▼                                  ▼                             │
│  ┌──────────────────┐              ┌──────────────────┐                     │
│  │ [Exploiter-1]    │              │ [Exploiter-4]    │                     │
│  │ WORKER           │  ···         │ WORKER           │                     │
│  │ Extract PoC      │              │ Create repro.sh  │                     │
│  └──────────────────┘              └──────────────────┘                     │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────┐              │
│  │ Exploiter Phase Aggregate Result (4 workers)              │              │
│  │ • repro.sh triggers expected sanitizer → SUCCESS          │              │
│  │ • Otherwise → FAIL                                        │              │
│  └───────────────────────────────────────────────────────────┘              │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────┘             │
│  │                                                                          │
│  ▼                                                                          │
│  ┌────────────────────┐                                                     │
│  │ [Fixer] MANAGER    │                                                     │
│  │ (Depth 1)          │                                                     │
│  └─────────┬──────────┘                                                     │
│            │                                                                │
│            ├──────────────────────────────────┐                             │
│            │                                  │                             │
│            ▼                                  ▼                             │
│  ┌──────────────────┐              ┌──────────────────┐                     │
│  │ [Fixer-1]        │              │ [Fixer-4]        │                     │
│  │ WORKER           │  ···         │ WORKER           │                     │
│  │ Analyze root     │              │ Validate patch   │                     │
│  │ cause            │              │                  │                     │
│  └──────────────────┘              └──────────────────┘                     │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────┐              │
│  │ Fixer Phase Aggregate Result (4 workers)                  │              │
│  │ • model_patch.diff exists AND repro.sh no error → SUCCESS │              │
│  │ • Otherwise → FAIL                                        │              │
│  └───────────────────────────────────────────────────────────┘              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

                              Agent Tree Structure
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│                              BOSS (Depth 0)                                 │
│                                   │                                         │
│            ┌──────────────────────┼──────────────────────┐                  │
│            │                      │                      │                  │
│            ▼                      ▼                      ▼                  │
│     ┌────────────┐         ┌────────────┐         ┌────────────┐            │
│     │  Builder   │         │ Exploiter  │         │   Fixer    │            │
│     │  MANAGER   │         │  MANAGER   │         │  MANAGER   │            │
│     │ (Depth 1)  │         │ (Depth 1)  │         │ (Depth 1)  │            │
│     └─────┬──────┘         └─────┬──────┘         └─────┬──────┘            │
│           │                      │                      │                  │
│     ┌─────┴─────┐          ┌─────┴─────┐          ┌─────┴─────┐            │
│     │ 5 WORKERS │          │ 4 WORKERS │          │ 4 WORKERS │            │
│     │ (Depth 2) │          │ (Depth 2) │          │ (Depth 2) │            │
│     └───────────┘          └───────────┘          └───────────┘            │
│                                                                             │
│     Total: 1 BOSS + 3 MANAGERS + 13 WORKERS = 17 agents per CVE instance   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Key Difference: Single Agent vs. Agent Tree

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Architectural Comparison                                 │
├────────────────────────────┬────────────────────────────────────────────────┤
│        SEC-bench (SB)      │           Arise Sec Lion (ASL)                 │
├────────────────────────────┼────────────────────────────────────────────────┤
│                            │                                                │
│  Builder (1 Agent)         │  Builder Tree (1 Manager + 5 Workers)          │
│       ↓                    │       ↓                                        │
│  Exploiter (1 Agent)       │  Exploiter Tree (1 Manager + 4 Workers)        │
│       ↓                    │       ↓                                        │
│  Fixer (1 Agent)           │  Fixer Tree (1 Manager + 4 Workers)            │
│                            │                                                │
│  Total: 3 Agents           │  Total: 17 Agents                              │
│                            │                                                │
├────────────────────────────┼────────────────────────────────────────────────┤
│  Model: GPT-4o, Claude     │  Model: Claude Sonnet (workers via Claude Code)│
│  Tool: SWE-agent, OpenHands│  Tool: Claude Code                             │
├────────────────────────────┼────────────────────────────────────────────────┤
│  repro.sh: Generated       │  repro.sh: Uses secb_sh from dataset           │
│  by Exploiter agent        │  (pre-defined in JSON)                         │
├────────────────────────────┼────────────────────────────────────────────────┤
│  Docker: Built at runtime  │  Docker: Pre-built (hwiwonlee DockerHub)       │
│  by Preprocessor           │                                                │
└────────────────────────────┴────────────────────────────────────────────────┘
```

---

## 3. Experimental Comparison Methodology

### 3.1 Variables

| Variable Type | Variable | Description |
|---------------|----------|-------------|
| **Independent (Manipulated)** | Agent Architecture | Single-agent (SB) vs. Tree-structured (ASL) |
| **Dependent (Measured)** | Success Rate | Per-phase and end-to-end success rates |
| | Cost | Total API cost per instance (USD) |
| | Duration | Time to complete all phases (seconds) |
| **Control (Held Constant)** | CVE Instances | Same dataset from HuggingFace |
| | Docker Environment | Same pre-built Docker images |
| | Build Script | Same build.sh content |
| | Success Criteria | Same sanitizer error matching |
| | LLM Backbone | Claude (3.5 Sonnet for fair comparison) |

### 3.2 What You're Actually Comparing

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Comparison Framework                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Research Question:                                                         │
│  "Does a tree-structured multi-agent approach outperform single-agent       │
│   approach for security vulnerability reproduction and patching tasks?"     │
│                                                                             │
│  Hypothesis:                                                                │
│  Tree structure enables task decomposition, allowing specialized workers    │
│  to focus on smaller subtasks, potentially improving success rate.          │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  Metrics to Compare:                                                        │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Primary Metrics                                                     │   │
│  ├─────────────────────────────────────────────────────────────────────┤   │
│  │ • Builder Success Rate:   (Successful Builds / Total Instances)     │   │
│  │ • Exploiter Success Rate: (Successful PoCs / Total Instances)       │   │
│  │ • Fixer Success Rate:     (Successful Patches / Total Instances)    │   │
│  │ • End-to-End Success:     (All 3 Phases Success / Total Instances)  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Secondary Metrics                                                   │   │
│  ├─────────────────────────────────────────────────────────────────────┤   │
│  │ • Cost per Instance (USD)                                           │   │
│  │ • Duration per Instance (seconds)                                   │   │
│  │ • Token Usage (input/output tokens)                                 │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Important Note: Not Apple-to-Apple Comparison

Your current setup has differences beyond architecture:

| Aspect | SEC-bench | Arise Sec Lion | Controlled? |
|--------|-----------|----------------|-------------|
| Agent Structure | Single | Tree (17 agents) | ❌ Different |
| Exploiter's repro.sh | Generated | Uses secb_sh from data | ❌ Different |
| Worker Tool | SWE-agent/OpenHands | Claude Code | ❌ Different |
| Model | Various | Claude Sonnet | ❌ Different |
| Docker | Preprocessor builds | Pre-built images | ✓ Same |
| Dataset | SEC-bench HuggingFace | Same | ✓ Same |

**Recommendation:** Acknowledge these differences in your report. Your comparison shows:
- "Can our tree-structured system with Claude Code achieve comparable results to SEC-bench?"
- NOT: "Is tree structure better than single agent?" (too many confounding variables)

---

## 4. Experiment Configuration Validation

### 4.1 Current Configuration Assessment

| Aspect | Status | Notes |
|--------|--------|-------|
| BOSS decomposes into 3 phases | ✓ Correct | Builder → Exploiter → Fixer |
| Manager decomposes into workers | ✓ Correct | 5+4+4 = 13 workers |
| Uses pre-built Docker images | ✓ Correct | hwiwonlee/secb.eval.x86_64.* |
| Uses secb_sh from dataset | ⚠️ Different from SB | SB generates repro.sh; you use existing |
| Phase prefixes in descriptions | ✓ Correct | [Builder], [Exploiter], [Fixer] |
| Deliverables match SB | ✓ Correct | Same files expected |
| Success criteria match SB | ✓ Correct | Sanitizer error matching |

### 4.2 Potential Issues

1. **secb_sh Usage**: Your system uses `secb_sh` from the dataset directly. This is **different from SB** where the Exploiter agent writes the repro logic. This makes your Exploiter task easier.

2. **No Preprocessor Phase**: You skip the Preprocessor and use pre-built Docker images. This is fine for comparison as long as both systems use the same images.

3. **Claude Code Tool**: Different from SWE-agent/OpenHands used in SB benchmarks. This is a confounding variable.

### 4.3 Recommendations

1. **Clearly document** that you're using pre-existing `secb_sh` for reproduction
2. **Consider two experiment modes**:
   - Mode A: Use secb_sh (current) - easier, tests build/fix capability
   - Mode B: Generate repro.sh - matches SB exactly, tests full pipeline
3. **Report both component and end-to-end success rates** for fair comparison

---

## 5. Interpreting Results from `secbench_result.jsonl`

Since the result file doesn't exist yet, here's how to interpret it when you have results:

### 5.1 Expected JSONL Format (based on `BenchmarkResult` model)

```jsonl
{"instance_id":"gpac.cve-2023-2838","builder":{"stage":"builder","success":true,"details":"Build successful","duration_seconds":120.5},"exploiter":{"stage":"exploiter","success":true,"details":"PoC triggered sanitizer","duration_seconds":85.2},"fixer":{"stage":"fixer","success":false,"details":"Patch failed validation","duration_seconds":200.1},"total_cost_usd":1.25,"overall_success":false}
{"instance_id":"another.cve-2024-xxxx","builder":{"stage":"builder","success":true,...},...}
```

### 5.2 How to Calculate Success Rates

```python
import json

results = [json.loads(line) for line in open('secbench_result.jsonl')]

total = len(results)
builder_success = sum(1 for r in results if r.get('builder', {}).get('success', False))
exploiter_success = sum(1 for r in results if r.get('exploiter', {}).get('success', False))
fixer_success = sum(1 for r in results if r.get('fixer', {}).get('success', False))
e2e_success = sum(1 for r in results if r.get('overall_success', False))

print(f"Builder SR:   {builder_success}/{total} = {100*builder_success/total:.1f}%")
print(f"Exploiter SR: {exploiter_success}/{total} = {100*exploiter_success/total:.1f}%")
print(f"Fixer SR:     {fixer_success}/{total} = {100*fixer_success/total:.1f}%")
print(f"End-to-End:   {e2e_success}/{total} = {100*e2e_success/total:.1f}%")
```

### 5.3 Comparison with SEC-bench Leaderboard

| Metric | SEC-bench Best (Claude 3.7 + OpenHands) | Your Target |
|--------|----------------------------------------|-------------|
| PoC Generation (Exploiter) | 18.0% | Compare |
| Vulnerability Patching (Fixer) | 34.0% | Compare |
| End-to-End | ~6% (18% × 34%) | Compare |

### 5.4 What Each Result Means

| Pattern | Interpretation |
|---------|----------------|
| Builder ✗ | Environment/build issue - instance may be problematic |
| Builder ✓, Exploiter ✗ | PoC crafting failed - may need better vulnerability analysis |
| Builder ✓, Exploiter ✓, Fixer ✗ | Patch generation failed - root cause analysis or patch validation issue |
| All ✓ | Complete success - instance fully processed |

---

## 6. Summary

### 6.1 Your Understanding: Validated ✓

Your understanding is correct with minor clarifications:
- `repro.sh` is a shell script, not a function
- You correctly identified that your system uses `secb_sh` from data instead of generating it

### 6.2 Key Differences from SEC-bench

1. **Architecture**: Tree-structured (17 agents) vs. Single-agent (3)
2. **Tooling**: Claude Code vs. SWE-agent/OpenHands
3. **Reproduction**: Uses pre-existing secb_sh vs. generates repro.sh

### 6.3 Valid Comparison

Your experiment can answer: "Can our tree-structured Claude Code system achieve comparable security task performance to published SEC-bench results?"

It cannot definitively answer: "Is tree structure better?" (due to other confounding variables)

### 6.4 Benchmark Data Quality

An instance passes the full pipeline if:
1. ✓ Builds successfully with sanitizers
2. ✓ PoC triggers the exact sanitizer error
3. ✓ Patch removes the error without breaking functionality

These instances represent high-quality, verified security vulnerabilities with reproducible fixes.

---

## References

- [SEC-bench Paper (arXiv:2506.11791)](https://arxiv.org/abs/2506.11791)
- [SEC-bench Leaderboard](https://sec-bench.github.io/)
- [SEC-bench Dataset (HuggingFace)](https://huggingface.co/papers/2506.11791)
