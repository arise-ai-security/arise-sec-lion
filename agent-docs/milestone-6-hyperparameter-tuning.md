# Milestone 6: Automatic Hyperparameter Tuning

**Status**: Not Started
**Prerequisites**: Milestones 1-5 complete

**Goal**: Enable parent-controlled retry with intelligent parameter adjustment.

---

## Implementation Steps

1. Create `AdjustmentStrategy` protocol
2. Implement `DefaultAdjustmentStrategy`
3. Add `decide_child_retry()` to parent
4. Add config section

---

## 6.1 Adjustment Strategy Interface

**File**: `core/domain/adjustment_strategy.py` (new)

```python
from typing import Any, Protocol


class AdjustmentStrategy(Protocol):
    """Strategy for adjusting hyperparameters on failure."""

    def suggest_adjustments(
        self,
        failure_category: str,
        current_config: dict[str, Any],
        attempt_number: int,
    ) -> dict[str, Any]:
        """Return adjusted config for retry."""
        ...


class DefaultAdjustmentStrategy:
    """Rule-based adjustment strategy."""

    def __init__(
        self,
        fallback_models: list[str] | None = None,
        timeout_multiplier_base: float = 1.5,
        max_timeout_multiplier: float = 4.0,
    ):
        self.fallback_models = fallback_models or ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]
        self.timeout_multiplier_base = timeout_multiplier_base
        self.max_timeout_multiplier = max_timeout_multiplier

    def suggest_adjustments(
        self,
        failure_category: str,
        config: dict[str, Any],
        attempt: int,
    ) -> dict[str, Any]:
        if failure_category == "timeout":
            multiplier = min(
                self.timeout_multiplier_base ** attempt,
                self.max_timeout_multiplier,
            )
            return {"timeout_multiplier": multiplier}

        elif failure_category == "model_error":
            model_index = min(attempt - 1, len(self.fallback_models) - 1)
            return {"model": self.fallback_models[model_index]}

        elif failure_category == "rate_limit":
            return {"delay_before_retry": 2 ** attempt}

        return {}
```

---

## 6.2 Parent Retry Decision

**File**: `core/domain/model.py`

Add method to `AgentSession`:
```python
def decide_child_retry(
    self,
    child_id: UUID,
    failure_category: str,
    child_retry_count: int,
    adjustment_strategy: AdjustmentStrategy,
) -> dict[str, Any] | None:
    """Parent decides whether to retry child and with what params.

    Returns adjusted config or None if no retry.
    """
    if child_retry_count >= self.retry_policy.max_retries:
        return None

    original_config = self._get_child_config(child_id)
    return adjustment_strategy.suggest_adjustments(
        failure_category,
        original_config,
        child_retry_count + 1,
    )

def _get_child_config(self, child_id: UUID) -> dict[str, Any]:
    """Get original config for a child agent."""
    for event in self.events:
        if isinstance(event, ChildSpawned) and event.child_id == child_id:
            return event.child_config
    return {}
```

---

## 6.3 Execution Service Integration

**File**: `core/application/execution_service.py`

```python
class AgentExecutionService:
    def __init__(
        self,
        ...,
        adjustment_strategy: AdjustmentStrategy | None = None,
    ):
        self._adjustment_strategy = adjustment_strategy or DefaultAdjustmentStrategy()

    async def _handle_child_failure(
        self,
        parent: AgentSession,
        child: AgentSession,
    ) -> None:
        """Handle child failure, potentially scheduling retry with adjusted params."""
        if parent.retry_policy is None:
            return

        adjusted_config = parent.decide_child_retry(
            child_id=child.session_id,
            failure_category=child.last_failure_category,
            child_retry_count=child.retry_count,
            adjustment_strategy=self._adjustment_strategy,
        )

        if adjusted_config:
            # Schedule retry with adjusted config
            await self._schedule_child_retry(parent, child, adjusted_config)
        else:
            # Max retries exceeded, propagate failure to parent
            parent.handle_child_failure(
                child_id=child.session_id,
                reason=child.error_message,
                retry_count=child.retry_count,
                category=child.last_failure_category,
            )
```

---

## 6.4 Configuration

**File**: `config/settings.py`

```python
class HyperparameterTuningConfig(BaseModel):
    """Configuration for automatic hyperparameter tuning."""

    enabled: bool = True
    strategy: Literal["default", "learned"] = "default"
    fallback_models: list[str] = ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]
    timeout_multiplier_base: float = Field(default=1.5, gt=1.0)
    max_timeout_multiplier: float = Field(default=4.0, gt=1.0)
```

**File**: `config/config.yaml`
```yaml
application:
  hyperparameter_tuning:
    enabled: true
    strategy: default  # "default" or "learned" (future)
    fallback_models:
      - gpt-4o
      - gpt-4-turbo
      - gpt-3.5-turbo
    timeout_multiplier_base: 1.5
    max_timeout_multiplier: 4.0
```

---

## Future: Learned Strategy

The `"learned"` strategy (future enhancement) could:
- Track success/failure patterns per model/task type
- Use historical data to predict best adjustments
- Implement bandit algorithms for exploration/exploitation

```python
class LearnedAdjustmentStrategy:
    """ML-based adjustment strategy (future)."""

    def __init__(self, history_store: HistoryStore):
        self._history = history_store

    def suggest_adjustments(
        self,
        failure_category: str,
        config: dict[str, Any],
        attempt: int,
    ) -> dict[str, Any]:
        # Query historical success rates
        # Apply bandit algorithm to select best adjustment
        # Track outcome for future learning
        ...
```

---

## Testing Strategy

Key test scenarios:
- Timeout failure → timeout multiplier increased
- Model error → fallback to next model in list
- Rate limit → exponential backoff delay
- Max retries → no further adjustments, failure propagates
- Custom strategy can be injected

## Files to Modify/Create

| File | Action |
|------|--------|
| `core/domain/adjustment_strategy.py` | Create new |
| `core/domain/model.py` | Add decide_child_retry(), _get_child_config() |
| `core/application/execution_service.py` | Add adjustment_strategy, _handle_child_failure() |
| `config/settings.py` | Add HyperparameterTuningConfig |
| `config/config.yaml` | Add hyperparameter_tuning section |
| `bootstrap/application.py` | Wire adjustment_strategy |
