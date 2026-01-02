# Design Choice 4: Thinker Justification Context Passing

This design builds on top of Design Choice 3 (Proportional Budget Allocation) by introducing rich context passing between thinker agents (supervisors) and their child agents.

## Problem Statement

**Need**: Better inter-agent communication and collaboration.

Based on observations from the [Budgeted Tree Plus system](./budgeted_tree_plus.md#interactive-diagram), thinkers and workers narrowly focus on their own subtasks without fully understanding the bigger picture. This leads to:

- **Misalignment**: Workers may not grasp the rationale behind their assignments
- **Redundant work**: Sub-agents lack awareness of how their work relates to siblings
- **Poor transitions**: PENDING → WORKER transitions suffer from insufficient context
- **Lost intent**: Strategic reasoning from parent thinkers doesn't reach execution level

## Strategy Overview

Pass down **structured justification context** from thinker agents to their child agents. This justification includes:

| Field | Purpose |
|-------|---------|
| Supervisor's Original Task | The parent's objective for full context |
| Subtask Objective | What this specific child should achieve |
| Assignment Rationale | Why this task was delegated to a sub-agent |
| Suggested Approach | Recommended execution strategy |
| Feasibility Reasoning | Why the suggested approach should work |
| Expected Deliverables | Concrete outputs expected from the child |
| Budget Allocation Context | Numeric weights and percentage allocation |

### Supervisor Expectations Format

```xml
<SUPERVISOR_EXPECTATIONS>
Your supervisor assigned this task with the following context and expectations:

**Supervisor's Original Task**: BuilderAgent for CVE-gpac.cve-2023-0770: Build gpac
environment using provided Dockerfile, build.sh, and work directory /src/gpac.

**Objective for This Subtask**: Establish a proper Docker build environment with all
required components and correct version checkout for gpac.

**Why This Was Assigned to You**: Separates environment setup from build execution;
the creation of a Docker image with correct context is a distinct preparatory step.

**Suggested Approach**: Utilize the provided Dockerfile to clone the gpac repository
at commit 514a3af977f675bd917e19f957fe6fb56ac14bf4, set /src/gpac as the working
directory, and integrate the supplied build.sh script.

**Why This Should Work**: Direct use of provided Dockerfile and build.sh ensures that
the environment is configured consistently with known working parameters.

**Expected Deliverables**: A Docker image ready for building gpac with
AddressSanitizer support, confirming that the environment setup is correctly executed.

## Budget Allocation Context
Your supervisor has allocated resources for this task with the following reasoning:

**Budget Allocation**: 40% of total project budget (weight 1.6 of total 4.0 across 2
subtasks)

**Complexity Assessment**: MODERATE: Involves integrating provided build context and
verifying correct Docker configuration.

**Significance/Priority**: HIGH: A proper build environment is critical for subsequent
build validation.

**Resource Justification**: Allocating 40% recognizes the importance and moderate
complexity of setting up a Docker-based build system to ensure consistency for the
compilation process.

Use this context to calibrate your effort:
- Higher budget % indicates more thorough work expected
- The complexity assessment tells you expected difficulty
- Significance helps prioritize quality vs. speed
</SUPERVISOR_EXPECTATIONS>
```

## Implementation Details

### SubtaskJustification Value Object

```python
# core/domain/values/subtask.py
class SubtaskJustification(BaseModel):
    """Justification for a subtask with budget reasoning (Design Choice 4).

    Provides rich context about why a subtask was created and how
    resources should be allocated.
    """

    model_config = {"frozen": True}

    # Core justification fields
    objective: str = ""              # What this subtask achieves
    plan: str = ""                   # How it will be executed
    split_reason: str = ""           # Why this was split from parent
    why_it_works: str = ""           # Why the approach should succeed
    expected_results: str = ""       # Concrete deliverables expected

    # Budget-related fields
    budget_allocation: str = ""      # e.g., "40% of budget (weight 1.6 of 4.0)"
    complexity_assessment: str = ""  # e.g., "MODERATE: Requires 3 operations"
    significance_weight: str = ""    # e.g., "HIGH: Critical path task"
    resource_justification: str = "" # e.g., "Needs tool execution + validation"
```

### Context Cascade

Justifications cascade down the agent tree, ensuring child agents receive context from all ancestors:

```
BOSS (original task: "Exploit CVE-2024-50665")
  │
  └─► MANAGER (task: "Analyze vulnerability path")
        │
        │  Generates justification for child:
        │  - Supervisor's Task: "Analyze vulnerability path"
        │  - Objective: "Map execution flow to NULL-deref"
        │  - Plan: "1) Load source 2) Trace call stack..."
        │  - Budget: "40% (weight 0.8 of 2.0)"
        │
        └─► WORKER receives full context
              │
              │  Understands:
              │  - Why this task matters
              │  - How it relates to parent's goal
              │  - Expected resource usage
              │  - Concrete deliverables
```

### LLM Prompt Integration

When a thinker decomposes tasks, the LLM generates justifications that are passed to child prompts:

```python
# Decomposition output includes justification
{
    "description": "Map execution flow to NULL-deref",
    "budget_weight": 1.6,
    "justification": {
        "objective": "Deliver exhaustive list of code conditions...",
        "plan": "1) Load drm_sample.c around line 1562\n2) Trace call stack...",
        "split_reason": "Thorough static analysis requires concentrated skills...",
        "why_it_works": "gpac's parser is open-source and well-commented...",
        "expected_results": "File 'path_map.md' with stack trace, MP4 boxes...",
        "budget_allocation": "40% of budget (weight 1.6 of total 4.0)",
        "complexity_assessment": "MODERATE: Multi-file code tracing",
        "significance_weight": "CRITICAL PATH: Subsequent spec depends on this",
        "resource_justification": "Static analysis demands line-by-line reasoning"
    }
}
```

## Benefits

### 1. Reduced Redundant Work

Sub-agents understand why their task was assigned, preventing overlap with siblings:

```
Parent Task: "Analyze CVE-2024-50665"
  │
  ├─► Child 1: "Map execution path" ← Knows to focus on code flow only
  │     (split_reason: "Separates path analysis from spec design")
  │
  └─► Child 2: "Draft MP4 spec"    ← Knows path data will be provided
        (split_reason: "Authoring spec distinct from reverse engineering")
```

### 2. Improved PENDING → WORKER Transitions

Rich context helps complexity evaluation make better decisions:

- **Before**: Agent sees only task description
- **After**: Agent sees objective, plan, expected complexity, and resource allocation

### 3. Enhanced Work Alignment

Sibling agents understand how their work relates to each other:

```
Siblings at same level:
  ├─► Worker 1: "Setup build env" (40% budget, HIGH significance)
  └─► Worker 2: "Run test suite"  (60% budget, CRITICAL PATH)
        │
        └─► Knows Worker 1's setup is prerequisite
```

### 4. Cascading Context

Lower-level thinkers inherit and extend justifications from ancestors:

```
BOSS Justification → MANAGER Justification → WORKER Prompt
     (original)          (extended)           (complete)
```

## Visual Example

The following shows a worker task card with full justification context:

| Field | Value |
|-------|-------|
| **Task** | [PathAnalyzerWorker] For CVE-2024-50665 in gpac, examine src/isomedia/drm_sample.c around line 1562... |
| **Status** | 40% budget, completed |
| **Objective** | Deliver an exhaustive list of code conditions, variable states, and MP4 input fields/boxes... |
| **Plan** | 1) Load drm_sample.c ±200 lines around 1562. 2) Trace call stack... 3) Review git history... |
| **Split Reason** | Thorough static/diff analysis requires concentrated reverse-engineering skills distinct from spec design |
| **Why It May Work** | gpac's parser is open-source and well-commented; commit diffs highlight same variables |
| **Expected Results** | File 'path_map.md' with stack trace, MP4 boxes/flags, value ranges, rationale |
| **Budget Allocation** | 20% of total project budget (weight 0.4 of total 2.0 across all subtasks) |
| **Complexity** | MODERATE: Requires multi-file code tracing and diff comparison but no build work |
| **Significance** | CRITICAL PATH: Subsequent spec drafting depends entirely on this factual mapping |
| **Resource Justification** | Static analysis with diff review demands line-by-line reasoning; 20% ensures adequate tokens |

## Configuration

No additional configuration required. Justification context passing uses the existing:

```yaml
orchestration:
  complexity_budget:
    enabled: true
    initial_amount: 1000.0
    threshold_ratio: 0.02
```

When budget is enabled, justifications automatically include budget allocation context.

## Events

Justification data is captured in existing decomposition events:

| Event | Purpose |
|-------|---------|
| `TasksDecomposed` | Records subtasks with full justification |
| `ComplexityBudgetAllocated` | Records budget given to child with context |

## Relationship to Other Design Choices

| Design Choice | Relationship |
|---------------|--------------|
| **Design Choice 2** (Budget Threshold) | Provides budget values for justification |
| **Design Choice 3** (Proportional Allocation) | Provides weight calculations for budget context |
| **Design Choice 4** (This) | Passes justification context to enable informed execution |

### Evolution of Context

```
DC2: "You have 182 budget units"
       │
       ▼
DC3: "You have 182 units (18% of parent's 1000, weight 1.0 of 5.5)"
       │
       ▼
DC4: "You have 182 units because:
      - Parent is analyzing CVE-2024-50665
      - Your objective is to map the execution path
      - This was split off because path analysis is distinct from spec design
      - You should trace call stacks and diff commits
      - This will work because gpac is open-source with good docs
      - Deliver path_map.md with stack trace and MP4 field mappings
      - Your 18% reflects moderate complexity on critical path"
```

## See Also

- [Design Choice 2: Complexity Budgeted Tree](./budgeted_tree.md) - Base budget threshold
- [Design Choice 3: Proportional Budget Allocation](./budgeted_tree_plus.md) - Weight-based allocation
- [Design Choice 5: Worker Report Context](./worker_report_context.md) - Bottom-up context passing
- [Context Passing Mechanism](./context-passing-mechanism.md) - Underlying context system
- [Context Passing Guide](./context-passing-guide.md) - Practical usage patterns
