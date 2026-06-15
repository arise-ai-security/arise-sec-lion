# Agentic Tree Topology System: Technical Research Document

A comprehensive design specification for building an AI-powered recursive multi-agent tree system. Grounded in the architecture and implementation of the **arise-sec-lion** platform.

> **As-built source of truth:** this is a design/research document. For the verified
> as-built system (event catalog, aggregates, runtime, criteria, with `file:line`
> citations) see [`SYSTEM_REFERENCE.md`](../SYSTEM_REFERENCE.md). Where this document and
> the code disagree, the code (and `SYSTEM_REFERENCE.md`) win.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Node Taxonomy](#2-node-taxonomy)
3. [Core Operations](#3-core-operations)
4. [Dependency & Priority Resolution (Q1)](#4-dependency--priority-resolution-q1)
5. [Success & Failure Criteria (Q2)](#5-success--failure-criteria-q2)
6. [Auto-Healing Strategy (Q3)](#6-auto-healing-strategy-q3)
7. [Context Passing Protocol (Q4)](#7-context-passing-protocol-q4)
8. [System-Level Hyperparameters](#8-system-level-hyperparameters)
9. [Event Sourcing & Full Projection](#9-event-sourcing--full-projection)
10. [Minimal Context Principle](#10-minimal-context-principle)
11. [Extended Questions & Deep Analysis](#11-extended-questions--deep-analysis)

---

## 1. System Overview

The system is a **recursive, self-healing multi-agent tree**. A single user task enters at the root (BOSS), gets recursively decomposed into subtasks that form a DAG-constrained tree, and leaf WORKER nodes execute them using external AI tools. Every decision, tool call, and state transition is captured as an immutable event in an append-only event store.

```
User Task
    │
    ▼
  BOSS (root) ─── decomposes ──► [subtask₁, subtask₂, subtask₃]
                                       │
                  each subtask spawns a PENDING child
                                       │
                  single LLM call (assess_task) decides fate
                       │               │               │
                    WORKER          MANAGER          INFEASIBLE
                   (execute)     (decompose more)   (parent re-plans)
                      │               │
                   tool runs       recurse ──► [grandchild₁, ...]
                      │
                   result flows UP via Report
                   context flows DOWN via Briefing
                   siblings share via Handoff + SharedStore
```

### Design Principles

1. **Event Sourcing as the single source of truth** — State is never mutated directly. Every node's state is reconstructed by replaying its immutable domain events.
2. **Hexagonal Architecture (Ports & Adapters)** — The core domain never imports infrastructure. Concrete implementations are injected at bootstrap time through protocol-defined ports.
3. **Minimal context, maximal precision** — Each node receives only the information necessary to complete its task, preventing hallucination from context overload.
4. **Fail-fast with structured recovery** — Failures are detected immediately and propagated up with enough context for the parent to make an informed re-planning decision.

---

## 2. Node Taxonomy

### 2.1 Roles

| Role | Purpose | Created When |
|------|---------|-------------|
| **BOSS** | Root agent. Always decomposes. Never executes directly. | System initialization |
| **PENDING** | Newly spawned child. Awaiting complexity evaluation. | After parent decomposes |
| **MANAGER** | Intermediate node. Further decomposes its assigned task. | PENDING assesses task as "complex" |
| **WORKER** | Leaf node. Executes task using external AI tools. | PENDING assesses task as "simple" |

### 2.2 Status Lifecycle

```
PENDING → ANALYZING ──┬── IN_PROGRESS → COMPLETED
                      ├── WAITING ────→ COMPLETED
                      ├── FAILED ─────→ ANALYZING (retry)
                      └── FAILED
```

| Status | Meaning |
|--------|---------|
| `PENDING` | Created, no task assigned yet |
| `ANALYZING` | Processing (LLM call in flight or about to execute) |
| `IN_PROGRESS` | Worker actively executing via tool adapter |
| `WAITING` | Manager/Boss waiting for all children to report |
| `COMPLETED` | Terminal — finished successfully |
| `FAILED` | Terminal — finished with error (may be retried) |
| `BLOCKED` | Blocked on an unsatisfied dependency |

### 2.3 The Decision Point (assess_task)

Every child is born as `PENDING`. A single LLM call (`assess_task`) produces an `AssessmentResult` with `action ∈ {execute, decompose, infeasible}`:

- **`execute`** → Node becomes WORKER. Gets picked up by the execution loop.
- **`decompose`** → Node becomes MANAGER. Immediately spawns its own children.
- **`infeasible`** → Node declares the task impossible under current constraints. Parent is notified and can re-decompose with different strategy.

This single-call decision avoids costly multi-round negotiation at each level.

---

## 3. Core Operations

The system has exactly **three orchestrator operations** (no pipeline abstraction, no strategy pattern for the operations themselves):

### 3.1 assess_task (PENDING → WORKER or MANAGER)

```
Build assessment prompt (Jinja2 templates)
  → Optional reconnaissance (read-only tool calls: file search, code analysis)
  → Single LLM call
  → Parse response: execute | decompose | infeasible
  → Emit ComplexityEvaluated event
```

**Reconnaissance phase**: Before deciding, the agent can use **read-only tools** (file readers, code searchers, symbol analyzers) to inspect the codebase. This is the "No Write Tool Call" phase — the agent gathers information but cannot modify anything. A configurable `ReconPolicy` per role/domain controls whether recon is enabled and how many iterations are allowed.

### 3.2 evaluate_task (BOSS/MANAGER → decompose → spawn children)

```
Build decomposition prompt with task + context
  → LLM produces JSON array of subtasks
  → Each subtask has: description, config, depends_on, success_criteria,
    target_paths, symbols, search_hints
  → Validate against topology limits (max_children, max_total_agents)
  → Apply soft limits (max_depth → force remaining subtasks to WORKER)
  → Emit SubtasksDefined + ChildSpawned* events
  → Parent transitions to WAITING
```

### 3.3 execute_task (WORKER → tool execution → verification)

```
Build worker prompt with briefing + sibling context + shared decisions
  → Delegate to external tool adapter (Claude Code / OpenHands / Google ADK)
  → Stream execution events (ThoughtCaptured*)
  → Run 4-stage verification pipeline
  → Emit WorkCompleted or WorkFailed
```

---

## 4. Dependency & Priority Resolution (Q1)

> **Question**: How can we make the system know the priority and dependency between workers so that it can execute left-to-right?

### 4.1 The Core Insight: LLM-Declared Dependencies as a DAG

Dependencies are **declared by the decomposing LLM at subtask creation time**, not discovered at execution time. When a BOSS or MANAGER decomposes a task, the LLM's JSON response includes a `depends_on: list[int]` field per subtask — these are 0-indexed sibling indices.

```python
class Subtask(BaseModel):
    description: str
    config: dict[str, Any]
    depends_on: list[int] = []          # e.g., [0, 1] = must wait for subtasks 0 and 1
    dependency_type: Literal["finish_to_start", "data", "none"] = "finish_to_start"
    estimated_complexity: Literal["simple", "complex", "unknown"] = "unknown"
    success_criteria: str = ""
```

Example decomposition:

```json
{
  "subtasks": [
    {"description": "Set up project structure",  "depends_on": [],    "config": {}},
    {"description": "Implement data models",     "depends_on": [0],   "config": {}},
    {"description": "Write unit tests",          "depends_on": [0],   "config": {}},
    {"description": "Integrate and test E2E",    "depends_on": [1, 2],"config": {}}
  ]
}
```

This creates the DAG:

```
  0 (setup)
  ├──► 1 (models)
  └──► 2 (tests)
         │
         ├──► 3 (integration)  ← waits for both 1 AND 2
         │
  1 ─────┘
```

### 4.2 Two Scheduling Modes

The system supports two scheduling strategies, selected automatically based on whether any `depends_on` edges exist:

#### DAG Mode (when `depends_on` edges exist)

Uses Kahn's algorithm for topological ordering. A worker is **ready** when all sibling indices in its `depends_on` set correspond to agents in terminal state (COMPLETED or FAILED). Workers with empty `depends_on` run immediately.

```python
class TaskScheduler:
    """Kahn's algorithm for DAG scheduling."""

    def get_ready(self) -> list[int]:
        """Return indices whose dependencies are all satisfied."""
        ready = []
        for idx, deps in self._pending_deps.items():
            if idx in self._completed or idx in self._failed:
                continue
            if deps.issubset(self._completed):
                ready.append(idx)
        ready.sort()  # Deterministic ordering
        return ready

    def invalidate_dependents(self, failed_index: int) -> list[int]:
        """BFS traversal to cascade failure to all transitive dependents."""
        # ...
```

#### Legacy Mode (no `depends_on` — pure left-to-right)

When no DAG edges exist, the system enforces **strict left-to-right, depth-first ordering**. This is implemented as a hierarchical path comparison:

1. Each agent gets a **hierarchical path** — a tuple of sibling indices from root to leaf. E.g., `(0, 1, 2)` = root's first child → its second child → its third child.
2. Pre-compute subtree completeness bottom-up in O(n) using memoization.
3. A worker is eligible only if **no left sibling has an incomplete subtree** (checked at every ancestor level).
4. Among all eligible workers, only the **leftmost** (lowest hierarchical path) executes.

```
      BOSS
     / | \
    M₀ M₁ M₂          hierarchical paths:
   /|   |   |\         W₀₀ = (0,0), W₀₁ = (0,1)
  W₀₀ W₀₁ W₁₀ W₂₀ W₂₁  W₁₀ = (1,0), W₂₀ = (2,0), W₂₁ = (2,1)

Execution order: W₀₀ → W₀₁ → W₁₀ → W₂₀ → W₂₁
(left-to-right, depth-first)
```

### 4.3 Prompt Engineering for Dependency Declaration

The key to making the LLM correctly declare dependencies is **prompt design**. The decomposition prompt template explicitly instructs the LLM:

1. **Order subtasks by execution dependency** — foundational work first (index 0), dependent work later (higher indices).
2. **Declare `depends_on` explicitly** — list which sibling indices must complete before this subtask can start.
3. **Specify `dependency_type`** — `finish_to_start` (most common), `data` (needs output from predecessor), or `none`.

### 4.4 Failure Cascade in DAGs

When a worker fails and exhausts retries, the `TaskScheduler.invalidate_dependents()` method performs a BFS traversal to find and fail all transitive dependents. This prevents the system from waiting indefinitely for tasks that can never run.

### 4.5 Key Design Decision: Why Not Runtime Dependency Discovery?

Static (LLM-declared) dependencies at decomposition time beat runtime discovery because:

- **Predictable scheduling**: The entire execution plan is visible before any worker starts.
- **Early cycle detection**: Kahn's algorithm detects cycles at decomposition time, before wasting compute.
- **Failure cascade**: Transitive invalidation is O(edges), not requiring complex runtime analysis.
- **Auditability**: The full DAG is captured in `SubtasksDefined` events and can be replayed/visualized.

---

## 5. Success & Failure Criteria (Q2)

> **Question**: How should we provide criteria of success/failure of the worker node, given that we don't know how the system will decompose and assign tasks?

### 5.1 The Core Insight: Parent Declares, System Verifies

The decomposing LLM **declares success criteria as part of the subtask definition**. Since the parent is the one who decided what the subtask should accomplish, it is in the best position to define what "done" means.

```python
class Subtask(BaseModel):
    description: str
    success_criteria: str = ""          # "Tests pass with 100% coverage on module X"
    failure_indicators: list[str] = []  # ["Compilation error", "Test failure"]
```

### 5.2 4-Stage Verification Pipeline

After a worker completes execution, its output passes through a **4-stage verification pipeline**:

| Stage | Type | What It Checks | Failure Action |
|-------|------|----------------|----------------|
| **1. Structural** | Deterministic | Output is non-empty and has meaningful content | Immediate fail |
| **2. Deterministic** | Rule-based | Domain-specific pattern matching (extensible) | Immediate fail |
| **3. Execution** | Rule-based | Runtime checks — exit codes, file existence (extensible) | Immediate fail |
| **4. LLM Judge** | AI-based | Evaluates output against `success_criteria` | Fail + feedback |

The LLM judge is the **final arbiter** and only runs when `success_criteria` is non-empty:

```
Prompt to judge:
  "You are a quality judge. Evaluate whether the worker's output
   satisfies the success criteria."

  ## Success Criteria
  {success_criteria from parent's subtask definition}

  ## Worker Report (structured summary)
  {task, artifacts, decisions, parent guidance}

  ## Work Output (terminal log, may be truncated)
  {head_tail(strip_ansi(result), 30000)}

  Response: {"passed": true/false, "feedback": "brief explanation"}
```

### 5.3 Hierarchical Success Propagation

Success is **defined recursively through the tree**:

```
A task T is SUCCESSFUL if and only if:
  - If T is a WORKER: All 4 verification stages pass
  - If T is a MANAGER/BOSS: ALL of T's children are SUCCESSFUL
```

When all children of a MANAGER report success, the manager auto-completes by aggregating their reports into a single `WorkCompleted` event. This propagates upward until the BOSS completes.

### 5.4 Failure Is Immediate But Recovery Is Structured

A **single child failure** triggers `handle_child_failure()` on the parent. The parent's response depends on the failure type:

| Failure Type | Parent Response |
|-------------|----------------|
| Worker execution failure | Parent fails (unless auto-heal recovers — see Q3) |
| Infeasible declaration | Parent re-decomposes (up to `max_redecompositions` times) |
| All retries exhausted | Parent fails, propagates up |

### 5.5 Making Success Criteria Effective Without Knowing Decomposition

The system addresses the "unknown decomposition" problem through several mechanisms:

1. **Cascading context**: Each subtask's `success_criteria` is written by the LLM that decomposed the parent task. The LLM has full visibility of the parent's task description, the overall plan, and all sibling subtasks, so it can write criteria that are specific yet achievable.

2. **Structured scoping hints**: The subtask includes `target_paths`, `symbols`, and `search_hints` that scope the worker's focus. The success criteria can reference these concrete artifacts.

3. **Parent justification propagation**: Each subtask carries a `justification: dict[str, str]` field (objective, plan, domain context) from the decomposing LLM. This gives the judge context about intent.

4. **The "sum = whole" invariant**: The decomposition prompt explicitly instructs: _"If all subtasks succeed, the parent task must be considered complete."_ This forces the LLM to write subtasks whose success criteria are collectively sufficient.

---

## 6. Auto-Healing Strategy (Q3)

> **Question**: If node A has children B, C, D (workers) and manager E (with worker children F, G), what should the retry criteria for node A be?

### 6.1 The Core Insight: Two Distinct Healing Mechanisms

The system distinguishes between **worker-level retry** (same task, different approach) and **parent-level re-decomposition** (different task breakdown entirely):

```
                          Node A (MANAGER)
                         /    |    |    \
                       B(W)  C(W)  D(W)  E(MGR)
                                         /    \
                                       F(W)   G(W)

Healing Levels:
  Level 1: Worker retry (B fails → retry B with escalated model)
  Level 2: Parent re-decomposition (E's children all fail → E re-decomposes)
  Level 3: Grandparent re-decomposition (E fails → A re-decomposes all of B,C,D,E)
```

### 6.2 Worker-Level Retry (Model Escalation + Circuit Breaker)

When a WORKER fails, the system *can* escalate through a **model escalation chain**.
The chain below is **illustrative** — the live default `model_escalation_chain` is an
**empty list** (`config/settings.py:372`, `default_factory=list`), so by default no model
escalation occurs and the worker retry budget collapses to the verification-retry cap (see
[`SYSTEM_REFERENCE.md`](../SYSTEM_REFERENCE.md) §II.6). A chain only takes effect if a
config explicitly sets one:

```yaml
retry:
  model_escalation_chain: []   # live default: empty (no escalation)
  # Example only — a deployment MAY configure something like:
  #   - "gpt-5.4-mini"
  #   - "gpt-5.4"
  circuit_breaker_threshold: 3
  circuit_breaker_reset_seconds: 300
```

**Algorithm**:

1. Worker fails → check `retry_count < len(model_escalation_chain)`.
2. If budget remaining: walk the chain from current model forward, skip circuit-broken models.
3. Emit `RetryScheduled(escalated_model)` → agent transitions back to `ANALYZING`.
4. The system loop picks it up and re-executes with the new model.
5. Circuit breaker: after N consecutive failures on a model, that model is "tripped" for a cooldown period.

**Only WORKERS are retried** — BOSS/MANAGER failures are structural (bad decomposition), handled by re-decomposition.

### 6.3 Parent Re-Decomposition (Infeasibility Recovery)

When a child declares `constraints_unsatisfiable` (i.e., the LLM determines the task is infeasible), a different recovery path activates:

1. Child emits `DecisionInfeasible(reason, constraint_details)`.
2. `ParentNotificationService` detects the "Infeasible:" prefix.
3. Parent's `trigger_redecomposition()` is called → clears current children, returns to `ANALYZING`.
4. Parent re-decomposes with the failure context injected into the prompt.
5. Capped at `max_redecompositions` per parent (default 2).

### 6.4 Concrete Answer: Retry Criteria for Node A

Given the tree:

```
A (MANAGER)
├── B (WORKER) ─── fails
├── C (WORKER)
├── D (WORKER)
└── E (MANAGER)
    ├── F (WORKER)
    └── G (WORKER)
```

The healing cascade proceeds:

| Event | System Response | Condition |
|-------|-----------------|-----------|
| B fails (attempt 1) | Retry B with next model in escalation chain | `B.retry_count < chain_length` AND model not circuit-broken |
| B fails (all retries) | B is terminal FAILED. Notify A. | Escalation chain exhausted |
| A receives B's failure | A checks: was it infeasible? | |
| → If infeasible | A re-decomposes (up to `max_redecompositions`). New subtasks replace B,C,D,E entirely. | `A.redecomposition_count < max_redecompositions` |
| → If not infeasible | A fails immediately. Propagates up to A's parent. | Default behavior |
| F fails inside E | Same worker retry logic applies to F | Independent of B,C,D |
| F exhausts retries | E fails. E notifies A with failure. | |
| E fails + B,C,D done | A aggregates: if any child failed, A fails | Unless infeasible re-decomposition is triggered |

**Key properties**:

1. **Worker retries are local** — B's retries don't affect C, D, or E.
2. **Failure propagation is immediate** — one child failure fails the parent (unless infeasible).
3. **Re-decomposition replaces the entire subtask set** — A doesn't retry individual children; it rethinks the entire plan.
4. **Budget is per-parent** — each manager gets its own `max_redecompositions` budget.
5. **DAG invalidation** — if B fails and D depends on B, D is also invalidated (transitively).

### 6.5 Extended Healing Strategies

Beyond the two core mechanisms, the system supports:

| Strategy | Trigger | Action |
|----------|---------|--------|
| Model escalation | Worker failure | Try more capable model |
| Temperature adjustment | Worker verification failure | Increase temperature for diversity |
| Re-decomposition | Infeasible declaration | Parent creates new subtask set |
| Scope narrowing | Repeated decomposition failure | Force remaining depth to WORKER |
| Global timeout | Wall-clock limit exceeded | Cancel deepest agents first (graceful) |

---

## 7. Context Passing Protocol (Q4)

> **Question**: What kind of data should be passed? Parent→child? Child→parent? Inter-child?

### 7.1 Three Communication Channels

The system defines a **discriminated union type** `NodeMessage` with three variants, each corresponding to a direction:

```python
NodeMessage = Briefing | Report | Handoff
# Discriminated on `direction` field: "down" | "up" | "lateral"
```

### 7.2 Briefing (↓ Parent → Child)

**When**: At child creation time (spawn).
**What**: Everything the child needs to understand its place in the hierarchy and its specific mandate.

```python
class Briefing(BaseModel):
    direction: Literal["down"] = "down"
    parent_task: str              # What the parent is trying to accomplish
    parent_role: str              # "boss" | "manager"
    ancestry: tuple[Ancestor, ...]  # Full lineage: (BOSS → MGR₁ → MGR₂ → ...)
    decisions: tuple[str, ...]    # Propagated architectural decisions
    subtask_justification: dict[str, str]  # "objective", "plan", "domain context"
```

**Design rationale**: The `ancestry` chain gives every node full lineage context without passing the entire tree state. Each `Ancestor` entry is lightweight:

```python
class Ancestor(BaseModel):
    agent_id: str
    role: str
    task_summary: str  # First 300 chars of task description
```

**What NOT to include**: Raw execution logs, full code outputs, or tool call transcripts from parent nodes. Only summaries and decisions. This is the "minimal context" principle in action.

### 7.3 Report (↑ Child → Parent)

**When**: On child completion (success or failure).
**What**: Structured result with enough context for the parent to aggregate or diagnose.

```python
class Report(BaseModel):
    direction: Literal["up"] = "up"
    task: str                    # What was assigned
    result: str                  # The actual output/outcome
    artifacts: tuple[str, ...]   # Named outputs (file paths, analysis results)
    decisions: tuple[str, ...]   # Decisions made during execution
    execution_summary: dict[str, Any]  # Duration, model used, token count, etc.
```

**Aggregation rule**: When all children report to a MANAGER, the manager aggregates all reports into a single `WorkCompleted` result that flows to its own parent. The aggregation is a concatenation of child results with structural headers.

### 7.4 Handoff (↔ Sibling ↔ Sibling)

**When**: Just before a worker executes (built fresh at execution time).
**What**: Status of all siblings + shared decisions from the hierarchy-wide SharedStore.

```python
class Handoff(BaseModel):
    direction: Literal["lateral"] = "lateral"
    parent_task: str | None         # Shared parent's goal
    current_sibling_index: int | None
    siblings: tuple[PeerStatus, ...]  # Status + result summary of each sibling
    shared_decisions: tuple[SharedDecision, ...]  # From SharedStore

    # Computed fields:
    total_siblings: int
    completed_count: int
    in_progress_count: int
    has_downstream_siblings: bool  # True if any peer executes after this worker
```

Each `PeerStatus` contains:

```python
class PeerStatus(BaseModel):
    agent_id: str
    sibling_index: int
    status: str             # pending, analyzing, in_progress, completed, failed
    task_summary: str       # First 200 chars
    result_summary: str | None  # First 2000 chars of result, only if completed
```

**Key design choice**: Completed siblings' results are truncated to 2000 chars (head/tail strategy). This gives the current worker enough context about what predecessors produced without flooding its context window.

### 7.5 SharedStore (Hierarchy-Wide Persistent Context)

Beyond the three directional channels, there is a **shared persistent context** per execution hierarchy:

```python
class SharedStore:
    artifact_store: ArtifactStore   # Shared files, code, analysis outputs
    decision_log: DecisionLog       # Architectural decisions (key/value/rationale)
```

**Write mechanism**: Workers embed `<context-update>` XML blocks in their output:

```xml
<context-update>
  <decision key="architecture" value="microservices" rationale="Scalability requirement" />
  <output key="api-schema" description="OpenAPI spec for user service">
    {...schema content...}
  </output>
</context-update>
```

The `ContextUpdateParser` extracts these, and the system persists them to the SharedStore. Subsequent siblings see these decisions in their Handoff context.

### 7.6 Data Flow Summary Table

| Channel | Direction | Sender | Receiver | Timing | Size Budget | Contents |
|---------|-----------|--------|----------|--------|-------------|----------|
| **Briefing** | ↓ Down | Parent | Child | At spawn | ~2KB | Ancestry chain, parent task, decisions, justification |
| **Report** | ↑ Up | Child | Parent | On completion | ~10KB | Result, artifacts, decisions, execution summary |
| **Handoff** | ↔ Lateral | System | Worker | Before execution | ~5KB per sibling | Peer statuses, shared decisions, result summaries |
| **SharedStore** | Global | Any worker | Any worker | Continuous | ~50KB total | Artifacts, architectural decisions |

---

## 8. System-Level Hyperparameters

### 8.1 Topology Constraints

```yaml
orchestration:
  topology:
    max_depth: 4                    # Soft limit — forces WORKER at this depth
    max_children_per_node: 8        # Hard limit — LLM output capped
    max_total_agents: 25            # Hard limit — global agent budget
```

- **`max_depth`** is a **soft limit**: when reached, remaining subtasks are forced to become WORKERs regardless of the LLM's assessment. This prevents infinite decomposition.
- **`max_children_per_node`** and **`max_total_agents`** are **hard limits**: if the LLM tries to create more subtasks than allowed, the system truncates.

### 8.2 Model Configuration (Per-Role)

The per-role `model` field is **required** in the Settings schema (no default —
`config/settings.py:81,97,182`) and is supplied per deployment / experiment cell.
Current experiment cells run the **gpt-5.4 family** (e.g. B3/B4: boss/manager `gpt-5.4`,
worker `gpt-5.4-mini` — `experiments/b4-boss-manager-worker/configs/B4-boss-manager-worker.yaml:36-39`).
Example shape:

```yaml
boss:
  model: "gpt-5.4"          # required; gpt-5.4 family in current cells
  temperature: 0.3
  max_tokens: 16000

manager:
  model: "gpt-5.4"          # required
  temperature: 0.3
  max_tokens: 16000

worker:
  model: "gpt-5.4-mini"     # required
  tool: "openhands"         # or "claude_code", "google_adk"
  timeout: 600
  max_iterations_per_run: 20
```

### 8.3 Retry & Resilience

```yaml
orchestration:
  max_retries: 3
  max_run_duration_seconds: 1800    # 30-minute hard stop for entire run
  max_redecompositions: 2           # Per-parent re-planning budget
  retry:
    model_escalation_chain: []      # live default: empty (illustrative chain below)
    # e.g. ["gpt-5.4-mini", "gpt-5.4"] if a deployment opts into escalation
    circuit_breaker_threshold: 3
    circuit_breaker_reset_seconds: 300
```

### 8.4 Concurrency

```yaml
orchestration:
  concurrency:
    max_concurrent_workers: 3       # Parallel worker executions
    max_concurrent_llm_calls: 5     # LLM API concurrency
    llm_jitter_max_ms: 500          # Random jitter to prevent thundering herd
```

### 8.5 Reconnaissance (Tool Calling)

```yaml
orchestration:
  tool_calling:
    max_iterations: 5               # Max tool-call rounds per assessment
    result_char_limit: 6000         # Truncate each tool result
    condense_after_iteration: 2     # LLM-summarize older exchanges after this
    token_budget: 80000             # Hard ceiling on accumulated context
```

### 8.6 Safeguard Summary

| Parameter | Default | Type | Purpose |
|-----------|---------|------|---------|
| `max_depth` | 4 | Soft | Forces WORKER beyond this depth |
| `max_children_per_node` | 8 | Hard | Caps subtask count per decomposition |
| `max_total_agents` | 25 | Hard | Global agent budget for entire hierarchy |
| `max_run_duration_seconds` | 1800 | Hard | Wall-clock timeout; deepest-first cancellation |
| `max_redecompositions` | 2 | Hard | Per-parent infeasibility re-planning cap |
| `max_iterations_per_run` | 20 | Hard | Worker tool-calling iteration cap |
| `circuit_breaker_threshold` | 3 | Hard | Consecutive failures before model is blocked |

---

## 9. Event Sourcing & Full Projection

### 9.1 Why Event Sourcing?

Every node's decision, tool call, and state transition must be logged. Event sourcing gives us:

1. **Complete audit trail** — Every action is an immutable fact.
2. **Time-travel debugging** — Replay events to reconstruct state at any point.
3. **Projections** — Build arbitrary read models from the same event stream (CQRS).
4. **Conflict resolution** — Optimistic Concurrency Control via sequence numbers.

### 9.2 Event Categories (35 event types)

35 types are registered in `EVENT_TYPE_REGISTRY` (`infrastructure/adapters/postgres_event_store.py:51-96`). They populate **two** event-sourced aggregates: `AgentSession` (primary) and `SharedStore` (the three Shared Context events below).

| Category | Events |
|----------|--------|
| **Lifecycle** | `AgentCreated`, `TaskAssigned`, `StatusChanged`, `ComplexityEvaluated` |
| **Decomposition** | `SubtasksDefined`, `ChildSpawned`, `ChildCompleted`, `ChildFailed` |
| **Execution** | `CodeGenerationStarted`, `ThoughtCaptured`, `WorkCompleted`, `WorkFailed` |
| **Verification/Retry** | `VerificationPassed`, `VerificationFailed`, `DecisionInfeasible`, `RedecompositionTriggered`, `RetryScheduled` |
| **Anti-leak** | `RuntimeSurfaceSealed` |
| **Observability** | `ProbeStarted/Completed`, `PromptSent`, `AgentExecutionStarted/Finished`, `OperationStarted/Finished`, `RunStarted/Completed` |
| **Cost** | `TokensConsumed`, `WorkerCostRecorded`, `LimitEnforced` |
| **Code-prefix cache** | `SourceFileObserved`, `SourceFileEdited` |
| **Shared Context** (`SharedStore`) | `SharedContextCreated`, `ArtifactStored`, `DecisionRecorded` |

### 9.3 Write Path

```
Domain method (e.g., agent.assign_task())
  → self._emit(event)
    → self._apply(event)      # Update in-memory state via singledispatchmethod
    → append to _changes      # Uncommitted event buffer
    → increment _sequence
  → event_store.append_batch(uncommitted_events, expected_version)
    → Atomic INSERT in single transaction
    → UNIQUE(aggregate_id, sequence_number) → ConcurrencyError on conflict
  → EventBroadcaster.publish()  # SSE push to subscribers
```

### 9.4 Read Path (Projections)

```
EventStore → Collector → Filter → Formatter → Sink

Projections available:
  - Summary: Overall run status and result
  - AgentList: All agents with roles, statuses, task descriptions
  - Cost: Token usage and cost breakdown by agent/model
  - AgentSummary: Detailed view of a single agent's lifecycle
```

### 9.5 Full Projection Capability

Because every action is an event, the system can reconstruct:

- **The complete decision tree**: Which agents decomposed what, and why.
- **The tool call trace**: Every tool invocation during reconnaissance, with inputs and outputs.
- **The execution timeline**: Exactly when each worker started, what model it used, and how long it took.
- **The context flow**: What briefing each node received, what report it sent, what decisions were shared.
- **The cost breakdown**: Token consumption per agent, per model, per operation type.

---

## 10. Minimal Context Principle

### 10.1 The Problem: Context Overload Causes Hallucination

LLMs have a fundamental trade-off: more context can improve understanding, but too much context degrades performance. The system is designed to provide **exactly the context needed** — no more, no less.

### 10.2 Context Management Strategies

| Strategy | Where Applied | Mechanism |
|----------|--------------|-----------|
| **Truncation** | Tool call results | Head/tail 6000 chars per result |
| **Observation masking** | Multi-round recon | After iteration 0, previous results replaced with 1-line stubs |
| **LLM summarization** | Accumulated tool context | After N iterations, older exchanges summarized by LLM |
| **Token budget ceiling** | Entire prompt | Hard ~80K token limit, break the loop if exceeded |
| **Result truncation** | Sibling handoffs | Completed sibling results capped at 2000 chars |
| **Ancestry compression** | Briefings | Only task summary (300 chars) per ancestor, not full results |
| **4-tier prompt layering** | All prompts | Each tier adds only role-relevant or operation-relevant content |

### 10.3 The Prompt Assembly Pipeline (4 Tiers)

```
Tier 1: System Identity       (prompts/system.j2)
  └─ "You are a multi-agent orchestration system..."
Tier 2: Role Persona           (prompts/roles/{boss,manager,worker,pending}.j2)
  └─ Role-specific behavior and constraints
Tier 3: Operation Instructions (prompts/operations/{assess,decomposition,execution}.j2)
Tier 4: Domain Extension       (optional, via PromptStrategy protocol)
  └─ Domain-specific templates (e.g., SEC-bench cybersecurity prompts)
```

Each tier adds **only what is relevant** to the current node's role and operation. A WORKER never sees the decomposition instructions. A BOSS never sees worker execution details.

---

## 11. Extended Questions & Deep Analysis

### Q1 Extended: Can the LLM Actually Produce Correct Dependencies?

**Empirical evidence**: The system sanitizes LLM outputs extensively (`SubtaskParser._sanitize_llm_subtask()`). Common failure modes:

| LLM Error | System Response |
|-----------|----------------|
| `depends_on: ["step 1"]` (string instead of int) | Parse to int, discard unparseable |
| Circular dependency | `TaskScheduler.has_cycle()` detects via Kahn's → reject decomposition |
| Out-of-range index | Silently removed (no sibling at that index) |
| Missing `depends_on` field | Default to empty list (no dependencies) |

**Improvement strategies**:

1. **Few-shot examples in the decomposition prompt** showing correct `depends_on` usage.
2. **Validation feedback loop**: If cycle detected, re-prompt with error message.
3. **Implicit dependency inference**: If subtask B references output from subtask A's description, infer the edge even if not declared. (Not yet implemented — requires NLI.)

### Q2 Extended: What If Success Criteria Are Too Vague?

The system has multiple fallback layers:

| Scenario | What Happens |
|----------|-------------|
| `success_criteria` is empty | LLM judge stage is skipped; only structural/deterministic/execution stages run |
| `success_criteria` is vague ("do a good job") | Judge prompt includes `report_context` with structured evidence (artifacts, decisions, parent guidance) to anchor evaluation |
| Judge can't parse LLM's response | Default to PASS (fail-open for judge parsing errors) |
| Domain-specific criteria needed | `PromptStrategy.extend_worker_prompt()` injects domain-specific verification context |

**Recommendations for stronger criteria**:

1. **Require testable predicates**: "File X exists and contains function Y" > "Code is written".
2. **Reference concrete artifacts**: "API endpoint returns 200 for payload Z" > "API works".
3. **Include negative criteria**: "No compilation errors. No security vulnerabilities detected by static analysis."
4. **Leverage `failure_indicators`**: The subtask field `failure_indicators: list[str]` can list explicit failure signals ("Segmentation fault", "Import error").

### Q3 Extended: Optimal Retry Budget Allocation

**The budget problem**: Given a fixed time/cost budget, how many retries should each level get?

**Current approach**: Retry budget = length of model escalation chain. This is simple but suboptimal.

**Recommended improvements**:

1. **Adaptive budgets based on depth**: Deeper nodes get fewer retries (they're smaller tasks; if they can't be done simply, the decomposition is likely wrong).

   ```
   retry_budget(node) = max(1, base_budget - depth)
   ```

2. **Cost-aware escalation**: Don't escalate to the most capable model for a trivial subtask. Use estimated complexity:

   ```
   if subtask.estimated_complexity == "simple":
       cap_escalation_at("gpt-5.4-mini")
   ```

3. **Time-budget redistribution**: If worker B completes quickly, redistribute its remaining time budget to worker D.

4. **Sibling failure correlation**: If 2/3 workers under the same parent fail, skip individual retries and go straight to parent re-decomposition (the decomposition itself is likely wrong).

### Q4 Extended: What Data Should NEVER Be Passed?

Equally important as what to pass is what to **exclude**:

| Never Pass | Reason |
|-----------|--------|
| Raw tool call transcripts to children | Massive context pollution; summaries suffice |
| Other subtrees' full execution logs | Irrelevant to current node's task |
| API keys / credentials in prompts | Security risk; use environment injection instead |
| Full file contents when summaries suffice | Context window waste |
| Previous failed attempts' full output | Only pass failure reason + feedback, not the raw output |

### Q5 (New): How Does the System Prevent Infinite Decomposition?

Multiple safeguards work together:

| Safeguard | Mechanism |
|-----------|-----------|
| `max_depth` (soft) | At max depth, force all subtasks to WORKER |
| `max_total_agents` (hard) | Refuse to spawn beyond global budget |
| `max_children_per_node` (hard) | Truncate oversized decompositions |
| `max_run_duration_seconds` | Hard wall-clock timeout, deepest-first cancellation |
| `max_redecompositions` | Cap re-planning cycles per parent |

### Q6 (New): How Should the System Handle Partial Success?

When some workers succeed and others fail:

1. **DAG mode**: Only invalidate transitive dependents of the failed worker. Independent branches continue.
2. **SharedStore preservation**: Successful workers' artifacts and decisions persist in the SharedStore, even if the overall run fails. A re-decomposition can leverage these.
3. **Partial result aggregation**: The parent's failure event includes which children succeeded and their results. A human operator (or a retry) can resume from partial progress.

### Q7 (New): How Do We Handle Non-Deterministic Worker Behavior?

The same worker with the same prompt might produce different results across runs:

1. **Verification pipeline as the arbiter**: The 4-stage pipeline doesn't care how the result was produced, only whether it meets criteria.
2. **Temperature as a healing lever**: On retry, the system can adjust temperature (higher for diversity after a failure, lower for precision).
3. **Event store as the record**: Every attempt is fully logged. The system never overwrites previous attempts — `RetryScheduled` creates a new execution trace, preserving the failed one.

### Q8 (New): What Happens When Two Subtrees Need to Coordinate?

Sibling subtrees (e.g., "build frontend" and "build backend") may need to share information:

1. **SharedStore**: The hierarchy-wide `DecisionLog` allows any worker to record decisions (e.g., "API schema is at /api/v1/users") visible to all other workers in the same run.
2. **Sibling result injection**: The Handoff mechanism includes completed siblings' result summaries. A backend worker can see the frontend worker's output summary.
3. **DAG edges across subtrees**: If tasks truly depend on each other, the decomposition should declare `depends_on` edges. Within the same parent's children, this is straightforward. Cross-subtree dependencies require the common ancestor to decompose at a granularity that captures the dependency.

### Q9 (New): How Should the Prompt Instruct the LLM to Decompose Well?

Effective decomposition is the single most impactful factor in system performance. Key prompt principles:

1. **MECE (Mutually Exclusive, Collectively Exhaustive)**: Subtasks should not overlap and should fully cover the parent task.
2. **Concrete over abstract**: "Implement the UserRepository class with CRUD methods" > "Handle data persistence".
3. **Explicit dependency declaration**: "This subtask requires the output of subtask 0" with `depends_on: [0]`.
4. **Success criteria as part of decomposition**: Force the LLM to define "done" at decomposition time, not at execution time.
5. **Complexity estimation**: The LLM should predict `estimated_complexity` to help the system decide depth vs. breadth.
6. **Scope scoping**: `target_paths`, `symbols`, `search_hints` narrow the worker's focus to specific code locations.

### Q10 (New): How Do We Measure and Optimize System Performance?

The event store enables rich analytics:

| Metric | Derived From | Optimization Lever |
|--------|-------------|-------------------|
| **Decomposition quality** | Ratio of worker successes to total workers | Improve decomposition prompts |
| **Retry rate** | `RetryScheduled` events / total workers | Better success criteria or model selection |
| **Context efficiency** | Token consumption per successful worker | Tighter context budgets |
| **Time to completion** | `RunCompleted.duration_seconds` | Parallelism, concurrency limits |
| **Cost per task** | `TokensConsumed` + `WorkerCostRecorded` events | Model selection, depth limits |
| **Re-decomposition rate** | `RedecompositionTriggered` events / total managers | Decomposition prompt quality |
| **Depth utilization** | Actual max depth / configured max depth | Topology tuning |

---

## Appendix A: System Loop Pseudocode

```
ExecutionService.run_system_loop(root_id):
│
├── while True:
│   ├── Check wall-clock deadline (cancel deepest-first if exceeded)
│   ├── Collect completed asyncio tasks (log exceptions)
│   ├── get_active_agent_ids(root_id, sequential_workers=True)
│   │   ├── Load all hierarchy events (recursive CTE, one SQL query)
│   │   ├── Build AgentSummaryReadModel per agent from events
│   │   ├── Non-workers (BOSS/MANAGER/PENDING) → always eligible
│   │   └── Workers → DAG scheduling or left-to-right ordering
│   │
│   ├── For each active agent → create asyncio.Task:
│   │   ├── Apply jitter (avoid thundering herd)
│   │   ├── Acquire LLM semaphore (concurrency limit)
│   │   └── run_agent_step(agent_id):
│   │       ├── Load agent (replay events from store)
│   │       ├── Dispatch by role:
│   │       │   ├── PENDING → assess_task (recon + decide)
│   │       │   ├── BOSS/MANAGER → evaluate_task (decompose)
│   │       │   └── WORKER → execute_task (tool + verify)
│   │       ├── Persist events (OCC retry on conflict)
│   │       └── Post-step:
│   │           ├── Spawn children if decomposed
│   │           ├── Extract context updates → SharedStore
│   │           ├── Notify parent if complete (recursive up)
│   │           ├── Try retry if failed (model escalation)
│   │           └── Notify parent if failed (infeasible → re-decompose)
│   │
│   ├── If no active agents and no in-progress tasks → break
│   └── sleep(poll_interval)
│
└── Emit RunCompleted on BOSS with duration/stats
```

## Appendix B: Key Source Files Reference

| Component | File |
|-----------|------|
| Aggregate (state machine) | `core/domain/aggregates/agent_session.py` |
| Domain events (35 types) | `core/domain/events/events.py` (registry: `infrastructure/adapters/postgres_event_store.py:51-96`) |
| NodeMessage union | `core/domain/values/node_message.py` |
| Subtask model | `core/domain/values/subtask.py` |
| Enums (Role, Status) | `core/domain/values/enums.py` |
| Task scheduler (Kahn's DAG) | `core/domain/services/task_scheduler.py` |
| Subtask parser | `core/domain/services/subtask_parser.py` |
| Context update parser | `core/domain/services/context_update_parser.py` |
| SharedStore aggregate | `core/domain/shared_context.py` |
| Orchestrator (3 operations) | `core/application/agent_orchestrator.py` |
| Execution service (loop) | `core/application/execution_service.py` |
| Query service (scheduling) | `core/application/services/query/query_service.py` |
| Prompt builder | `core/application/services/prompt_builder.py` |
| Tool calling service (recon) | `core/application/services/tool_calling_service.py` |
| Context condenser | `core/application/services/context_condenser.py` |
| Child factory | `core/application/services/child_factory.py` |
| Parent notifier | `core/application/services/parent_notifier.py` |
| Verification pipeline | `core/application/services/orchestration/verification_pipeline.py` |
| Hierarchy limits | `core/domain/values/limits.py` |
| Config (Pydantic Settings) | `config/settings.py` |

## Appendix C: Glossary

| Term | Definition |
|------|-----------|
| **Aggregate** | DDD concept — a cluster of domain objects treated as a single unit for state changes. There are **two** event-sourced aggregates: `AgentSession` (primary) and `SharedStore`. |
| **Assess** | The single LLM call that decides whether a PENDING node should execute (WORKER) or decompose (MANAGER). |
| **Briefing** | Downward context message from parent to child at spawn time. |
| **Circuit Breaker** | A model is temporarily blocked after N consecutive failures to prevent wasted compute. |
| **DAG** | Directed Acyclic Graph — the dependency structure between sibling workers. |
| **Domain Event** | An immutable fact about something that happened. 35 types cover the entire system's state transitions. |
| **Handoff** | Lateral context message carrying sibling statuses and shared decisions. |
| **Hierarchy** | The complete tree of agents spawned from a single BOSS root. |
| **Infeasible** | A node's declaration that its assigned task cannot be completed under current constraints. |
| **Model Escalation** | Retrying a failed worker with a more capable (typically more expensive) LLM model. |
| **OCC** | Optimistic Concurrency Control — assume no conflict, detect at write time via version. |
| **Projection** | A read model built by replaying events. Multiple projections can exist for the same event stream. |
| **Recon / Reconnaissance** | Read-only tool calling during assessment to gather information before making the execute/decompose decision. |
| **Re-decomposition** | A parent discarding its current children and creating a new subtask set after an infeasible failure. |
| **Report** | Upward context message from child to parent on completion. |
| **SharedStore** | Hierarchy-wide persistent context (artifacts + decisions) accessible to all agents in a run. |
| **Subtask** | A unit of work created by decomposition. Becomes a child agent. |
| **Topology** | The shape constraints of the agent tree: max depth, max children, max total agents. |
