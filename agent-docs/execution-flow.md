# Execution Flow

## Overview

`main.py` → `bootstrap.main()` → argparse → CLI command dispatch.

For `run <task>`:
1. Bootstrap wires all dependencies (PostgresEventStore, LiteLLM, worker adapter)
2. CLI creates root BOSS agent
3. System loop polls active agents until all terminal
4. Results display and cleanup

## System Loop

```
CLI.run_with_task(task)
  ├── _initialize_infrastructure()     → event_store.connect()
  ├── _bootstrap_boss_agent(task)      → create_boss_agent()
  ├── _run_orchestration_loop(root_id) → run_system_loop()
  ├── _display_final_result()
  └── _cleanup()
```

`AgentExecutionService.run_system_loop()`:
```python
while True:
    active_agents = get_active_agent_ids()   # ANALYZING or WAITING
    if not active_agents: break
    for agent_id in active_agents:           # DAG-aware ordering
        run_agent_step(agent_id)
    await asyncio.sleep(poll_interval)
```

## Agent Step

`run_agent_step(agent_id)`:
1. Load agent from event store (replay events)
2. Dispatch based on role/status to `AgentOrchestrator`:
   - PENDING → `evaluate_complexity(agent)`
   - BOSS/MANAGER (ANALYZING) → `evaluate_task(agent)`
   - WORKER → `execute_task(agent, ...)`
3. Persist new events with OCC (retry on `ConcurrencyError`)
4. Post-step: spawn children, notify parent

## Three Orchestrator Operations

### evaluate_complexity (PENDING → WORKER/MANAGER)

```
Build complexity prompt (Jinja2) → LLM query → parse simple/complex
→ agent.apply_complexity_result()
→ Events: TokensConsumed, ComplexityEvaluated
```

### evaluate_task (BOSS/MANAGER → decompose → spawn)

```
Build decomposition prompt → LLM query → parse subtasks JSON
→ check hard limits (max_children, max_total_agents)
→ apply soft limits (max_depth → force WORKER)
→ agent.apply_subtasks_and_spawn_children()
→ Events: TokensConsumed, SubtasksDefined, ChildSpawned*, StatusChanged→WAITING
```

If LLM responds `constraints_unsatisfiable` → `ConstraintFailure` → `DecisionInfeasible` event → parent re-decomposes.

### execute_task (WORKER → tool execution → verification)

```
Build worker prompt → run worker tool (Claude SDK / OpenHands / Google ADK)
→ stream events (ThoughtCaptured*)
→ 4-stage verification: structural → deterministic → execution → LLM judge
→ Events: CodeGenerationStarted, ThoughtCaptured*, WorkCompleted/WorkFailed
```

## Completion Propagation

```
WORKER completes
  → ParentNotificationService.notify_parent()
    → parent.handle_child_update(child_id, report)
    → if all children done → parent emits WorkCompleted
    → recurse up until BOSS completes
```

On child failure with "Infeasible:" prefix → triggers re-decomposition instead of failure propagation.

## Auto-Healing Retry

On worker failure:
1. Check model escalation chain in config
2. If next model available and circuit breaker not tripped → `RetryScheduled` event
3. Agent returns to ANALYZING with escalated model
4. Circuit breaker trips after N consecutive failures per model

## Key Files

| File | Role |
|------|------|
| `main.py` | Entry point |
| `bootstrap/bootstrap.py` | Argparse + wiring |
| `presentation/cli.py` | CLI orchestration |
| `core/application/execution_service.py` | Main loop, retry, concurrency |
| `core/application/agent_orchestrator.py` | 3 operations (no pipeline abstraction) |
| `core/application/services/child_factory.py` | Spawn children |
| `core/application/services/parent_notifier.py` | Completion propagation |
| `core/domain/aggregates/agent_session.py` | Event-sourced aggregate |
