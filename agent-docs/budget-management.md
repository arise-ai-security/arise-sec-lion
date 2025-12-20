# Budget & Resource Management

> **Document Type**: Technical Specification
> **Parent Document**: [TECHNICAL.md](../TECHNICAL.md)

This document details the budget allocation, reward/penalty mechanisms, and resource management system.

---

## Table of Contents

1. [Overview](#overview)
2. [Budget Allocation](#budget-allocation)
3. [Reward/Penalty Mechanism](#rewardpenalty-mechanism)
4. [Multi-Model Strategy](#multi-model-strategy)
5. [Task Queue Management](#task-queue-management)
6. [Verification System](#verification-system)
7. [Cost Tracking](#cost-tracking)
8. [Budget Events Reference](#budget-events-reference)

---

## Overview

### Purpose

The budget system provides:
- **Resource control**: Limit total computational spend
- **Incentive alignment**: Reward efficient agents, penalize failures
- **Early termination**: Stop losing strategies quickly
- **Fair allocation**: Distribute resources based on task complexity

### Key Concepts

| Concept | Description |
|---------|-------------|
| **Budget** | Abstract resource units allocated to agents |
| **Allocation** | Parent distributes budget to children |
| **Recollection** | Parent retrieves budget from completed children |
| **Reward Ratio** | Multiplier applied when recollecting (success = bonus) |
| **Penalty Ratio** | Multiplier applied when recollecting (failure = loss) |

---

## Budget Allocation

### Allocation Flow

```
BOSS (initial_budget=1000)
│
├─ SubtasksDefined { subtasks: [A, B, C] }
│
├─ Subtask A: "Database setup"
│   │
│   └─ SubordinatesSpawned { total_budget: 300 }
│       │
│       ├─ BudgetAllocated { agent: child_1, amount: 100, source: "parent" }
│       ├─ BudgetAllocated { agent: child_2, amount: 100, source: "parent" }
│       └─ BudgetAllocated { agent: child_3, amount: 100, source: "parent" }
│
├─ Subtask B: "Auth logic" → 300 budget
│
└─ Subtask C: "API routes" → 300 budget
│
└─ Remaining reserve: 100
```

### Allocation Rules

1. **Initial Allocation**: BOSS receives full budget at creation
2. **Equal Split**: Subordinates receive equal shares (by default)
3. **Reserve**: Parent may retain a reserve for retries
4. **Recursive**: Managers follow same rules when spawning children

### Budget Events

```python
# Initial allocation to BOSS
BudgetAllocated(
    aggregate_id=boss_id,
    amount=1000.0,
    source="initial"
)

# Parent allocates to child
BudgetAllocated(
    aggregate_id=child_id,
    amount=100.0,
    source="parent"
)
```

---

## Reward/Penalty Mechanism

### Ratios

| Outcome | Ratio | Rationale |
|---------|-------|-----------|
| **Success** | 1.2x | Incentivize efficient completion |
| **Terminated** | 1.0x | No penalty for early stop (sibling won) |
| **Failure** | 0.8x | Penalty for failed attempts |
| **All Failed** | 0.0x | Total loss when all siblings fail |

### Recollection Flow

```
Subordinates working on "Database setup":

┌─────────────────────────────────────────────────────────────────┐
│  Child 1 (Claude)                                               │
│  • Initial budget: 100                                          │
│  • Work completed: spent 20, remaining 80                       │
│  • Outcome: SUCCESS (first to complete)                         │
│  • Recollection: 80 × 1.2 = 96                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Child 2 (Gemini)                                               │
│  • Initial budget: 100                                          │
│  • Work in progress: spent 0, remaining 100                     │
│  • Outcome: TERMINATED (sibling won)                            │
│  • Recollection: 100 × 1.0 = 100                                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Child 3 (GPT-4o)                                               │
│  • Initial budget: 100                                          │
│  • Work in progress: spent 0, remaining 100                     │
│  • Outcome: TERMINATED (sibling won)                            │
│  • Recollection: 100 × 1.0 = 100                                │
└─────────────────────────────────────────────────────────────────┘

Total recollected: 96 + 100 + 100 = 296
Original allocation: 300
Net change: -4 (cost of successful work)
```

### Recollection Events

```python
BudgetRecollected(
    aggregate_id=parent_id,
    child_id=child_1_id,
    original_allocation=100.0,
    remaining_budget=80.0,
    ratio_applied=1.2,
    amount_recollected=96.0,
    child_succeeded=True
)

BudgetAdjusted(
    aggregate_id=parent_id,
    adjustment=296.0,
    reason="subtask_completion",
    new_balance=996.0
)
```

---

## Multi-Model Strategy

### Why Multiple Models?

Different LLMs have different strengths:
- **Claude**: Strong reasoning, careful analysis
- **GPT-4o**: Fast, good at common patterns
- **Gemini**: Multimodal, creative approaches

By spawning 3 subordinates with different models, we:
1. Increase probability of success
2. Get diverse solution approaches
3. Discover which models work best for task types

### Spawning Strategy

```
Subtask: "Implement database migrations"

┌────────────────────────────────────────────────────────────────┐
│                    SubordinatesSpawned                          │
│                                                                 │
│  subtask: { description: "Implement database migrations" }      │
│  total_budget_allocated: 300                                    │
│  subordinate_configs: [                                         │
│    { child_id: "...", model: "claude-sonnet", budget: 100 },   │
│    { child_id: "...", model: "gemini-pro", budget: 100 },      │
│    { child_id: "...", model: "gpt-4o", budget: 100 }           │
│  ]                                                              │
└────────────────────────────────────────────────────────────────┘
```

### First Success Wins

```
Timeline:
─────────────────────────────────────────────────────►

Child 1 (Claude):    [████████████████████] → SUCCESS
Child 2 (Gemini):    [████████░░░░░░░░░░░░] → TERMINATED
Child 3 (GPT-4o):    [██████░░░░░░░░░░░░░░] → TERMINATED

                              ↑
                    FirstSuccessRecorded
```

### Events

```python
FirstSuccessRecorded(
    aggregate_id=parent_id,
    winning_child_id=child_1_id,
    subtask=subtask,
    sibling_ids_terminated=[child_2_id, child_3_id],
    method_used="alembic migrations",
    result="Created 3 migration files",
    budget_recollected_from_winner=96.0,
    budget_recollected_from_siblings=200.0
)
```

---

## Task Queue Management

### Queue Structure

Each agent maintains a FIFO queue of subtasks:

```
Agent Task Queue:
┌─────────────────────────────────────────┐
│ HEAD → [Subtask A] → [Subtask B] → ...  │ → TAIL
└─────────────────────────────────────────┘
         ↑
    Next to process
```

### Queue Operations

| Operation | Event | Description |
|-----------|-------|-------------|
| **Enqueue** | `TaskEnqueued` | Add subtask to tail |
| **Dequeue** | `TaskDequeued` | Remove from head for processing |
| **Inject** | `TaskReinjected` | Insert at head (retry after failure) |

### Retry Flow

```
1. Subtask fails verification
2. TaskReinjected { subtask, verification_context, retry_count: 1 }
3. Subtask added to HEAD of queue (priority)
4. Next dequeue gets the retry

Queue before:  [B] → [C] → [D]
Queue after:   [A'] → [B] → [C] → [D]
               ↑
         Revised subtask with context
```

---

## Verification System

### Heuristic Triggers

Verification is injected based on:

| Heuristic | Trigger Condition | Estimated Cost |
|-----------|-------------------|----------------|
| **Complexity** | Large subtree (>5 agents) + complex task | High |
| **Random** | 10% probability per completion | Low |
| **Suspicious** | Edits don't match task complexity | Medium |
| **Time-based** | >10 min since last verification | Medium |
| **Budget** | Must have sufficient budget available | - |

### Verification Flow

```
1. Child completes subtask
2. VerificationHeuristicEvaluated { complexity_score: 0.8, random_roll: 0.05, ... }
3. Decision: verification_decided = true
4. VerificationInjected { target_subtask, target_child_id, injection_reason: "complexity" }
5. VerifierSpawned { verifier_id, verifier_config: { model: "claude-opus" } }
6. Verifier executes independently
7. VerificationCompleted { verification_passed: false, issues_found: [...] }
8. TaskReinjected { subtask, verification_context: "...", retry_count: 1 }
```

### Verifier Agent

The verifier is intentionally different from the original workers:
- **Different model**: Often a more capable model (e.g., Claude Opus)
- **Independent**: No shared state with original workers
- **Deep analysis**: More thorough verification approach

---

## Cost Tracking

### Token-Based Costs

Every LLM call records token usage:

```python
TokensConsumed(
    aggregate_id=agent_id,
    model="gpt-4o",
    prompt_tokens=1500,
    completion_tokens=500,
    total_tokens=2000,
    cost_usd=0.025,
    operation="complexity_evaluation"
)
```

### Worker Tool Costs

Worker tools (Claude Code, OpenHands) may have separate costs:

```python
WorkerCostRecorded(
    aggregate_id=agent_id,
    tool_name="claude_code",
    model="claude-sonnet-4",
    tokens=5000,
    cost_usd=0.15,
    duration_seconds=45.2
)
```

### Budget Enforcement

When total cost exceeds limit:

```python
BudgetExceeded(
    aggregate_id=agent_id,
    budget_limit_usd=10.0,
    current_total_usd=10.25,
    exceeded_by_usd=0.25
)
```

This triggers agent termination and propagates up the tree.

---

## Budget Events Reference

### Allocation Events

| Event | Fields | Description |
|-------|--------|-------------|
| `BudgetAllocated` | `amount`, `source` | Initial budget assigned |
| `BudgetAdjusted` | `adjustment`, `reason`, `new_balance` | Budget modified |

### Recollection Events

| Event | Fields | Description |
|-------|--------|-------------|
| `BudgetRecollected` | `child_id`, `remaining_budget`, `ratio_applied`, `amount_recollected`, `child_succeeded` | Parent recollects from child |
| `ChildFailed` | `child_id`, `failure_reason`, `budget_at_failure` | Child failed task |
| `AllChildrenFailed` | `subtask_description`, `child_ids`, `penalty_ratio` | All siblings failed |

### Multi-Model Events

| Event | Fields | Description |
|-------|--------|-------------|
| `SubordinatesSpawned` | `subtask`, `total_budget_allocated`, `subordinate_configs` | Multiple agents spawned |
| `FirstSuccessRecorded` | `winning_child_id`, `sibling_ids_terminated`, `budget_recollected_*` | First success |
| `AllSubordinatesFailed` | `subtask`, `failed_child_ids`, `failure_reasons`, `total_budget_lost` | All failed |

### Task Queue Events

| Event | Fields | Description |
|-------|--------|-------------|
| `TaskEnqueued` | `subtask` | Subtask added to queue |
| `TaskDequeued` | `subtask` | Subtask removed for processing |
| `SubtaskRetried` | `original_subtask`, `revised_subtask`, `retry_count`, `revision_reason` | Retry with context |

### Verification Events

| Event | Fields | Description |
|-------|--------|-------------|
| `VerificationInjected` | `target_subtask`, `target_child_id`, `injection_reason` | Verification triggered |
| `VerifierSpawned` | `verifier_id`, `target_subtask`, `verifier_config` | Verifier created |
| `VerificationCompleted` | `verification_passed`, `verification_report`, `issues_found` | Verification result |
| `TaskReinjected` | `subtask`, `verification_context`, `retry_count` | Failed verification retry |
| `VerificationHeuristicEvaluated` | `complexity_score`, `random_roll`, `suspicious`, `verification_decided` | Heuristic audit |

### Cost Events

| Event | Fields | Description |
|-------|--------|-------------|
| `TokensConsumed` | `model`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `operation` | LLM usage |
| `WorkerCostRecorded` | `tool_name`, `model`, `tokens`, `cost_usd`, `duration_seconds` | Worker tool cost |
| `BudgetExceeded` | `budget_limit_usd`, `current_total_usd`, `exceeded_by_usd` | Budget limit hit |
| `LimitEnforced` | `limit_type`, `limit_value`, `attempted_value`, `action_taken` | System limit applied |
