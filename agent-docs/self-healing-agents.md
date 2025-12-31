# Self-Healing Agents (Future Feature)

This document describes a planned feature for automatic recovery when LLM agents violate execution constraints.

## Context

Execution limits (`max_depth`, `max_children_per_node`, `max_total_agents`) are passed to LLMs in prompts. LLMs should decompose tasks to stay within these limits. However, LLMs may occasionally violate these constraints.

### Current Behavior (Phase 1)
When an LLM violates a limit:
1. `LimitEnforced` event is emitted
2. Agent fails with descriptive error message
3. Already-created children continue to completion
4. Sibling agents are unaffected

This "fail fast" approach makes LLM compliance issues visible and queryable.

## Proposed: Self-Healing Strategy (Phase 2)

Instead of failing immediately, the system could retry with adjusted parameters.

### Concept

```
LLM violates limit
       ↓
Emit warning event (LimitEnforced with action="retry_attempted")
       ↓
Adjust hyperparameters:
  - Lower temperature (more deterministic)
  - Add explicit constraint reminder to prompt
  - Optionally use more capable model
       ↓
Retry decomposition
       ↓
Success? → Continue normally
Failure? → Fall back to fail-fast
```

### Strategy Pattern

```python
class LimitViolationStrategy(Protocol):
    """Strategy for handling LLM limit violations."""

    async def handle(
        self,
        agent: AgentSession,
        violation: LimitViolation,
        orchestrator: AgentOrchestrator,
    ) -> LimitViolationResult:
        ...


class SelfHealingStrategy:
    """Retry with stricter prompt and adjusted hyperparameters."""

    max_retries: int = 2

    async def handle(self, agent, violation, orchestrator):
        for attempt in range(self.max_retries):
            adjusted_config = self._adjust_config(agent.config, violation, attempt)
            result = await orchestrator.retry_decomposition(
                agent=agent,
                config=adjusted_config,
                error_context=violation,
            )
            if result.success:
                return LimitViolationResult(action="retried_success")

        # Exhausted retries, fall back to fail
        agent.fail_with_reason("Retry exhausted after limit violation")
        return LimitViolationResult(action="retry_exhausted")

    def _adjust_config(self, config, violation, attempt):
        """Progressively stricter adjustments."""
        adjustments = [
            {"temperature": 0.3},  # First retry: lower temperature
            {"temperature": 0.1, "model": "more-capable-model"},  # Second: even stricter
        ]
        return apply_adjustments(config, adjustments[attempt])
```

### Configuration

```yaml
execution:
  limit_violation_strategy: "fail_fast"  # or "self_healing"
  self_healing:
    max_retries: 2
    temperature_decay: 0.5  # Multiply temperature by this on each retry
    fallback_model: null    # Optional more capable model for retries
```

### Events

New event types for observability:

```python
class LimitViolationRetried(DomainEvent):
    """LLM violated limit, retry attempted with adjusted config."""
    limit_type: str
    attempt: int
    adjusted_config: dict
    original_error: str


class LimitViolationRecovered(DomainEvent):
    """Self-healing retry succeeded."""
    limit_type: str
    attempts_needed: int
    final_config: dict
```

### Implementation Considerations

1. **Prompt Enhancement**: On retry, add explicit section:
   ```
   CRITICAL: Previous attempt violated {limit_type} limit.
   You MUST produce exactly {limit_value} or fewer {items}.
   This is attempt {attempt + 1} of {max_retries}.
   ```

2. **Cost Tracking**: Retries consume additional tokens. Track separately:
   ```python
   agent.emit_tokens_consumed(..., operation="retry_decomposition")
   ```

3. **Timeout Handling**: Retries should respect overall execution timeout.

4. **Model Selection**: System could automatically select models based on:
   - Task complexity
   - Previous failure patterns
   - Cost constraints

### Future Extensions

- **Learning**: Track which adjustments work for which violation types
- **Proactive**: Detect likely violations before they happen based on task analysis
- **Graceful Degradation**: For `max_total_agents`, could spawn fewer children with consolidated tasks

## Related

- `core/application/execution_service.py` - Current fail-fast implementation
- `core/domain/events.py` - `LimitEnforced` event
- `core/application/agent_orchestrator.py` - Decomposition logic
