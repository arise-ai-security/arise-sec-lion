# Design Choice 3: Complexity/Significance Computation

This design builds on top of the Complexity Budgeted Tree (Design Choice 2) by introducing **proportional budget allocation** based on subtask complexity and significance.

## Problem Statement

**Need**: Fair complexity budget allocation among child agents so that:
- Complex/critical subtasks can grow deeper in the tree
- Non-essential tasks (logging, documentation) have limited budget and finish quickly
- Resource allocation reflects actual work requirements

## Strategy Overview

Compute each subtask's **complexity** and **significance** to determine a `budget_weight`, then split the parent's complexity budget proportionally based on each subtask's weight.

### Allocation Formula

$$
\text{child\_budget} = \text{parent\_budget} \times \frac{\text{subtask\_weight}}{\sum \text{all\_weights}}
$$

### Example Calculation

If a supervisor agent has budget 1000 and creates 3 subtasks with weights 1.0, 3.0, and 1.5:

| Subtask | Weight | Calculation | Budget |
|---------|--------|-------------|--------|
| Subtask 1 | 1.0 | 1000 × (1.0 / 5.5) | **182** |
| Subtask 2 | 3.0 | 1000 × (3.0 / 5.5) | **545** |
| Subtask 3 | 1.5 | 1000 × (1.5 / 5.5) | **273** |
| **Total** | 5.5 | | **1000** |

Higher weights mean more resources for complex/critical tasks.

## Budget Weight Assignment

Budget weights are assigned by the LLM during task decomposition based on three factors:

### 1. Complexity (Primary Factor)

| Complexity Level | Weight Range |
|-----------------|--------------|
| Simple tasks | 0.5 - 1.0 |
| Moderate tasks | 1.0 - 2.0 |
| Complex tasks | 2.0 - 3.0 |

### 2. Importance (Critical Path Consideration)

| Importance Level | Weight |
|-----------------|--------|
| Normal tasks | 1.0 |
| Important tasks | 1.5 - 2.0 |
| Critical path tasks | 2.0 - 4.0 |

### 3. Estimated Effort (Relative Duration)

| Duration | Weight |
|----------|--------|
| Quick tasks | 0.5 - 1.0 |
| Average tasks | 1.0 |
| Long tasks | Proportional (2x time ≈ 2x weight) |

### Combined Examples

| Task Type | Weight | Rationale |
|-----------|--------|-----------|
| Research/documentation | 0.8 - 1.0 | Quick, low complexity |
| Standard implementation | 1.0 - 1.5 | Normal baseline |
| Core feature with tests | 2.5 - 3.0 | Complex + important |
| Security-critical code | 3.0 - 4.0 | High stakes, needs extra resources |

### Constraints

| Constraint | Value |
|------------|-------|
| Minimum weight | 0.1 |
| Maximum weight | 10.0 |
| Default (if unspecified) | 1.0 |

**Note**: Weights are relative. A subtask with weight 2.0 receives twice the budget of one with weight 1.0.

## Implementation Details

### Subtask Value Object

```python
# core/domain/values/subtask.py
class Subtask(BaseModel):
    """Immutable subtask with budget weight for proportional allocation."""

    description: str
    config: dict[str, Any]
    budget_weight: float = Field(default=1.0, ge=0.0)
    justification: SubtaskJustification
```

### Budget Calculation

```python
# core/domain/aggregates/agent_session.py
def calculate_child_budgets(self, subtasks: list) -> list[float]:
    """Calculate proportional budget allocation for children.

    child_budget = parent_budget * (weight / total_weights)
    """
    total_weight = sum(s.budget_weight for s in subtasks)

    return [
        self.complexity_budget * (s.budget_weight / total_weight)
        for s in subtasks
    ]
```

### Budget Recollection

When a child completes, unused budget can be recollected by the parent:

```yaml
# config/config.yaml
complexity_budget:
  success_reward_ratio: 1.2   # Recollect 1.2x remaining budget on success
  failure_penalty_ratio: 0.0  # Recollect 0x on failure (lose the budget)
```

```python
def recollect_child_budget(
    self,
    child_id: UUID,
    child_remaining_budget: float,
    success: bool,
    success_reward_ratio: float,
    failure_penalty_ratio: float,
) -> None:
    """Recollect budget from completed child with reward/penalty."""
    ratio = success_reward_ratio if success else failure_penalty_ratio
    amount_recollected = child_remaining_budget * ratio
    # Add to parent's budget pool
```

## Mathematical Properties

### Budget Conservation

The sum of complexity budgets of all child agents equals the parent's complexity budget:

$$
\sum_{i=1}^{n} \text{child\_budget}_i = \text{parent\_budget}
$$

### Transitive Property

The sum of all workers' complexity budgets equals their common ancestor's budget:

$$
\sum_{\text{workers}} \text{budget} = \text{boss\_initial\_budget}
$$

### Implications

1. **No Overallocation**: Workers never deal with more complex tasks than their budget allows
2. **Inspectable Distribution**: Easy to verify builder workers have higher total budget than documenter workers
3. **Proper Decomposition**: If workers have excessive budget, decomposition was insufficient

## Events

| Event | Purpose |
|-------|---------|
| `ComplexityBudgetAllocated` | Records budget given to child |
| `ComplexityBudgetRecollected` | Records budget returned from child |

### ComplexityBudgetRecollected Fields

| Field | Description |
|-------|-------------|
| `child_id` | ID of completed child |
| `original_allocation` | Budget originally given |
| `remaining` | Child's unused budget |
| `ratio_applied` | Success/failure ratio used |
| `amount_recollected` | Actual amount added back |

## Configuration

```yaml
orchestration:
  complexity_budget:
    enabled: true
    initial_amount: 1000.0
    threshold_ratio: 0.02

    # Design Choice 3: Recollection ratios
    success_reward_ratio: 1.2   # Reward efficiency
    failure_penalty_ratio: 0.0  # Penalize failure
```

## Visualization

```
BOSS (budget: 1000)
  │
  ├─► Core Feature (weight: 3.0, budget: 545)
  │     │
  │     ├─► Implementation (weight: 2.0, budget: 363)
  │     │
  │     └─► Tests (weight: 1.0, budget: 182)
  │
  ├─► Validation (weight: 1.0, budget: 182)
  │
  └─► Documentation (weight: 1.5, budget: 273)
        │
        └─► WORKER (budget: 273) ← Gets proportional share
```

## See Also

- [Design Choice 2: Complexity Budgeted Tree](./budgeted_tree.md) - Base budget threshold
- [Context Passing Mechanism](./context-passing-mechanism.md) - How budget flows through tree
- [Execution Flow](./execution-flow.md) - Pipeline that allocates budgets
