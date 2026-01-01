# Design Choice 2: Complexity Budgeted Tree

This design builds upon the naive tree-structured agentic system by introducing complexity budget management to control agent tree growth.

## Problem Statement

**Need**: Restrain tree growth. Naive tree generates a huge number of agents, leading to:
- Resource exhaustion
- Unfocused task execution
- Exponential agent proliferation

## Strategy Overview

Set a **complexity budget threshold** to restrict the spawning of new agents. When an agent's remaining budget falls below this threshold, it must become a WORKER to finish the current work rather than spawning children.

### Key Configuration Parameters

```yaml
# config/config.yaml
orchestration:
  complexity_budget:
    enabled: true                # Enable budget-based tree control
    initial_amount: 1000.0       # Starting budget for BOSS agent
    threshold_ratio: 0.02        # Force WORKER when budget < initial * ratio
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `enabled` | `false` | Toggle budget enforcement |
| `initial_amount` | `1000.0` | Budget allocated to BOSS agent |
| `threshold_ratio` | `0.02` | Percentage threshold (2% of initial) |

### Expected Worker Count

The number of workers in the tree can be estimated as:

```
Expected Workers ≈ 1 / threshold_ratio
```

For example:
- `threshold_ratio = 0.05` (5%) → ~20 workers
- `threshold_ratio = 0.02` (2%) → ~50 workers
- `threshold_ratio = 0.01` (1%) → ~100 workers

## How It Works

### Budget Flow

```
BOSS (budget: 1000)
  │
  ├─► MANAGER (budget: 500)
  │     │
  │     ├─► MANAGER (budget: 250)
  │     │     │
  │     │     └─► WORKER (budget: 125)  ← Below threshold (20), forced WORKER
  │     │
  │     └─► WORKER (budget: 250)
  │
  └─► MANAGER (budget: 500)
        │
        └─► WORKER (budget: 500)
```

### Threshold Check

At complexity evaluation, the `CheckBudgetThreshold` step checks:

```python
threshold = initial_boss_budget * threshold_ratio
if agent.complexity_budget < threshold:
    # Force WORKER role - no further decomposition allowed
```

### Implementation Details

**Pipeline Step**: `core/application/pipeline/steps/budget.py:CheckBudgetThreshold`

```python
class CheckBudgetThreshold:
    """Check if agent's complexity budget is below threshold.

    If enabled and budget < initial_amount * threshold_ratio, forces WORKER role.
    This is soft enforcement - the agent becomes a worker instead of spawning children.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        if agent.is_below_budget_threshold(
            initial_boss_budget=self._config.initial_amount,
            threshold_ratio=self._config.threshold_ratio,
        ):
            # Force WORKER role via LimitEnforced event
            agent.emit_limit_enforced(
                limit_type="complexity_budget",
                limit_value=int(threshold),
                attempted_value=int(agent.complexity_budget),
                action_taken="forced_worker_role",
            )
            # Short-circuit complexity evaluation
            agent.apply_complexity_result(
                complexity="simple",
                reasoning="Budget below threshold - forced WORKER role",
                determined_role=AgentRole.WORKER,
            )
```

## Relationship to Structural Limits

This complexity budget threshold is a **soft limit** that works alongside the existing **hard limits**:

| Limit Type | Enforcement | Behavior |
|------------|-------------|----------|
| `max_depth` | **Soft** | At depth limit, children forced to WORKER |
| `max_children_per_node` | **Hard** | Agent fails if too many subtasks |
| `max_total_agents` | **Hard** | Agent fails if exceeding global budget |
| `complexity_budget` | **Soft** | Below threshold → forced WORKER |

### Advantages Over Structural Limits

1. **Flexibility**: Adjustable via `threshold_ratio` without changing code
2. **Proportional Control**: Budget depletes proportionally to tree structure
3. **Predictable**: Easy to calculate expected worker count
4. **Adaptive**: Can be tuned based on project size

## Events

Budget-related events for observability:

| Event | Purpose |
|-------|---------|
| `ComplexityBudgetAllocated` | Records budget given to agent |
| `LimitEnforced` | Records when threshold forced WORKER |

## Configuration Example

For a small project (~10 workers):
```yaml
complexity_budget:
  enabled: true
  initial_amount: 100.0
  threshold_ratio: 0.10    # 10% → ~10 workers
```

For a large project (~50 workers):
```yaml
complexity_budget:
  enabled: true
  initial_amount: 1000.0
  threshold_ratio: 0.02    # 2% → ~50 workers
```

## See Also

- [Design Choice 3: Complexity Budget Plus](./budgeted_tree_plus.md) - Proportional budget allocation
- [Execution Flow](./execution-flow.md) - How agents process tasks
- [Domain Model](./domain-model.md) - Agent lifecycle and events
