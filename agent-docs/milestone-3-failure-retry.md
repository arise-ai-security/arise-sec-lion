# Milestone 3: Failure Handling & Retry

**Status**: Not Started
**Prerequisites**: Milestone 1 (Cost Tracking) ✅, Milestone 2 (System Limits) ✅

**Goal**: Retry failed workers with adjusted hyperparameters, notify parents.

---

## Implementation Steps

1. Add `ChildFailed`, `RetryScheduled`, `RetryStarted` events
2. Create `RetryPolicy` model
3. Add retry state to `AgentSession`
4. Implement `schedule_retry()`, `handle_child_failure()`
5. Add parent notification on failure
6. Implement `FailureCategorizer`

---

## 3.1 New Domain Events

**File**: `core/domain/events.py`

```python
class FailureCategory(str, Enum):
    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    RATE_LIMIT = "rate_limit"
    CONTENT_POLICY = "content_policy"
    UNKNOWN = "unknown"

class ChildFailed(DomainEvent):
    """Child agent failed, parent notified."""
    child_id: UUID
    reason: str
    retry_count: int
    failure_category: str

class RetryScheduled(DomainEvent):
    """Retry scheduled with adjusted parameters."""
    attempt_number: int
    delay_seconds: float
    adjustments: dict[str, Any]  # {"timeout_multiplier": 2.0, "model": "gpt-4-turbo"}
    failure_category: str

class RetryStarted(DomainEvent):
    """Retry attempt begun after delay."""
    attempt_number: int
```

---

## 3.2 Retry Policy Configuration

**File**: `core/domain/retry_policy.py` (new)

```python
class RetryAdjustment(BaseModel):
    timeout_multiplier: float = 1.5
    temperature_delta: float = 0.0
    fallback_model: str | None = None

class RetryPolicy(BaseModel):
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    exponential_base: float = 2.0
    max_delay_seconds: float = 60.0
    adjustments: dict[str, RetryAdjustment] = {}  # per failure category

    def get_delay(self, attempt: int) -> float:
        delay = self.base_delay_seconds * (self.exponential_base ** (attempt - 1))
        return min(delay, self.max_delay_seconds)
```

---

## 3.3 AgentSession Changes

**File**: `core/domain/model.py`

Add fields to `_initialize_defaults()`:
```python
self.retry_count: int = 0
self.retry_policy: RetryPolicy | None = None
self.pending_retry: bool = False
self.child_failures: dict[UUID, str] = {}
```

Add methods:
```python
def schedule_retry(self, failure_category: str) -> bool:
    """Schedule retry if policy allows. Returns True if scheduled."""

def start_retry(self) -> None:
    """Mark retry as started, reset status to ANALYZING."""

def handle_child_failure(self, child_id: UUID, reason: str, retry_count: int, category: str) -> None:
    """Record child failure, decide on propagation."""
```

Modify `is_terminal()`:
```python
def is_terminal(self) -> bool:
    if self.status == AgentStatus.COMPLETED:
        return True
    if self.status == AgentStatus.FAILED and not self.pending_retry:
        return True
    return False
```

---

## 3.4 Failure Categorizer

**File**: `core/application/execution_service.py`

```python
class FailureCategorizer:
    TIMEOUT_PATTERNS = ["timed out", "timeout"]
    RATE_LIMIT_PATTERNS = ["rate limit", "429"]
    MODEL_ERROR_PATTERNS = ["model not found", "invalid model"]

    @classmethod
    def categorize(cls, reason: str) -> str:
        # Pattern matching to determine category
```

---

## 3.5 Execution Service Retry Logic

**File**: `core/application/execution_service.py`

Modify `run_agent_step()`:
```python
# After detecting WorkFailed event:
if work_failed and agent.retry_policy:
    category = FailureCategorizer.categorize(work_failed.reason)
    if agent.schedule_retry(category):
        # Retry scheduled - continue loop
```

Modify `_handle_parent_notification()`:
```python
# Also notify parent on FAILED (not just COMPLETED):
if agent.status == AgentStatus.FAILED and not agent.pending_retry:
    parent.handle_child_failure(
        child_id=agent.session_id,
        reason=agent.error_message,
        retry_count=agent.retry_count,
        failure_category=agent.last_failure_category,
    )
```

---

## 3.6 Configuration

**File**: `config/config.yaml`
```yaml
application:
  retry_policy:
    max_retries: 3
    base_delay_seconds: 1.0
    exponential_base: 2.0
    adjustments:
      timeout:
        timeout_multiplier: 2.0
      model_error:
        fallback_model: gpt-4-turbo
      rate_limit:
        timeout_multiplier: 1.0
```

---

## Testing Strategy

Key test scenarios:
- Worker timeout → retry with 2x timeout → succeed
- Model error → fallback to gpt-4-turbo → succeed
- Max retries exceeded → parent notified → task fails
- Rate limit → exponential backoff → retry succeeds

## Files to Modify/Create

| File | Action |
|------|--------|
| `core/domain/events.py` | Add ChildFailed, RetryScheduled, RetryStarted |
| `core/domain/retry_policy.py` | Create new |
| `core/domain/model.py` | Add retry state and methods |
| `core/application/execution_service.py` | Add FailureCategorizer, retry logic |
| `config/settings.py` | Add RetryPolicyConfig |
| `config/config.yaml` | Add retry_policy section |
| `infrastructure/adapters/postgres_event_store.py` | Add new events to registry |
