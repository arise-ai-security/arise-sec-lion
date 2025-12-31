# Execution Flow

This document traces the complete execution path when running a task through the Arise Sec Lion system. When you execute a command like `docker compose --profile dev exec app-dev python main.py run "Create a fizzbuzz function"`, the system bootstraps all dependencies (PostgreSQL event store, LiteLLM adapter, worker tools), creates a root BOSS agent, and enters an orchestration loop that recursively decomposes complex tasks into subtasks. Each subtask spawns a PENDING agent that undergoes complexity evaluation via LLM to determine if it should become a WORKER (simple, execute directly) or MANAGER (complex, decompose further). Workers execute tasks using tools like Claude Code PTY, streaming their thought process as events. When workers complete, results propagate up the hierarchy through parent notifications until the root BOSS agent aggregates all results and the system exits.

---

## Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           USER COMMAND                                       │
│  docker compose exec app-dev python main.py run "Create fizzbuzz function"  │
└───────────────────────────────────────┬─────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  1. ENTRY POINT (main.py:25)                                                  │
│  ────────────────────────────────────                                         │
│  main() → bootstrap() → cli()                                                 │
│                                                                               │
│  Bootstrap layer wires all dependencies:                                      │
│    • PostgresEventStore (persistence)                                         │
│    • LiteLLMAdapter (LLM calls)                                              │
│    • ClaudeCodePTYAdapter / OpenHandsAdapter (worker tools)                  │
│    • AgentExecutionService (orchestration)                                    │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  2. CLI COMMAND (presentation/cli.py:211)                                     │
│  ───────────────────────────────────────                                      │
│  @cli.command() def run(task)                                                 │
│    → asyncio.run(app.run_with_task(task))                                     │
│                                                                               │
│  CLI.run_with_task():                                                         │
│    1. _initialize_infrastructure() → event_store.connect()                    │
│    2. _bootstrap_boss_agent(task) → create_boss_agent()                       │
│    3. _run_orchestration_loop(root_id) → run_system_loop() -> Main Logic      │
│    4. _display_final_result()                                                 │
│    5. _cleanup()                                                              │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  3. CREATE BOSS AGENT (execution_service.py:142)                              │
│  ───────────────────────────────────────────────                              │
│  create_boss_agent(task_description):                                         │
│    1. Generate UUID for root agent                                            │
│    2. Create working directory: ./output/{root_id}/                          │
│    3. Create AgentSession.create(role=BOSS)                                  │
│    4. agent.assign_task(task_description)                                    │
│    5. Persist AgentCreated + TaskAssigned events to PostgreSQL               │
│                                                                               │
│  Events Emitted:                                                              │
│    • AgentCreated(role=BOSS, parent_id=None)                                 │
│    • TaskAssigned(task_description="Create fizzbuzz function")               │
│                                                                               │
│  Agent State: role=BOSS, status=ANALYZING                                    │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  4. SYSTEM LOOP (execution_service.py:206)                                    │
│  ─────────────────────────────────────────                                    │
│  run_system_loop(root_agent_id):                                              │
│    while True:                                                                │
│      active_agents = get_active_agent_ids()  # status=ANALYZING/WAITING      │
│      if not active_agents: break                                              │
│      for agent_id in active_agents:                                           │
│        run_agent_step(agent_id)                                              │
│      await asyncio.sleep(poll_interval)                                       │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  5. AGENT STEP (execution_service.py:178)                                     │
│  ────────────────────────────────────────                                     │
│  run_agent_step(agent_id):                                                    │
│    1. Load agent from event store (replay events)                            │
│    2. Attach execution context (depth limits, child limits)                  │
│    3. Dispatch to AgentOrchestrator pipelines:                               │
│       • PENDING → complexity_pipeline.execute()                              │
│       • BOSS/MANAGER → decomposition_pipeline.execute()                      │
│       • WORKER → worker_pipeline.execute()                                   │
│    4. Persist new events with OCC (optimistic concurrency)                   │
│    5. Handle post-step: spawn children, notify parent                        │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
        ┌───────────────────────────────┴───────────────────────────────┐
        │                                                               │
        ▼                                                               ▼
┌───────────────────────────────────────┐   ┌───────────────────────────────────┐
│  6A. BOSS/MANAGER: TASK DECOMPOSITION │   │  6B. WORKER: TASK EXECUTION       │
│  ─────────────────────────────────────│   │  ──────────────────────────────── │
│  decomposition_pipeline steps:        │   │  worker_pipeline steps:           │
│                                       │   │                                   │
│  1. ValidateDecomposingAgent          │   │  1. ValidateWorkerAgent           │
│  2. ExtractExecutionContext           │   │  2. StartWorkerExecution          │
│  3. FetchRegisteredTasks              │   │  3. BuildWorkerPrompt             │
│  4. BuildDecompositionPrompt          │   │  4. EmitPromptSent                │
│  5. EmitPromptSent                    │   │  5. RunWorkerSession              │
│  6. QueryLLM                          │   │     • ClaudeCodePTYAdapter: spawn │
│  7. EmitTokensConsumed                │   │       claude CLI in PTY           │
│  8. ParseSubtasks                     │   │     • OpenHandsAdapter: API call  │
│  9. DeduplicateSubtasks               │   │                                   │
│  10. CheckLimitViolations (hard)      │   │  Events Emitted:                  │
│  11. DetermineChildRole (soft)        │   │    • CodeGenerationStarted        │
│  12. SpawnChildren                    │   │    • ThoughtCaptured (many)       │
│                                       │   │    • WorkCompleted/WorkFailed     │
│  Events Emitted:                      │   │                                   │
│    • TokensConsumed                   │   │  Agent State: status=COMPLETED    │
│    • SubtasksDefined                  │   │                                   │
│    • ChildSpawned (for each subtask)  │   │                                   │
│    • StatusChanged → WAITING          │   │                                   │
└───────────────────────────────────────┘   └───────────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  7. CHILD CREATION (child_factory.py)                                         │
│  ────────────────────────────────────                                         │
│  For each ChildSpawned event:                                                 │
│    1. Create AgentSession.create(role=PENDING, parent_id=boss_id)            │
│    2. Set parent context (ancestry chain, decisions, artifacts)              │
│    3. Create execution context (depth+1, limits)                             │
│    4. agent.assign_task(subtask.description)                                 │
│    5. Persist to event store                                                  │
│                                                                               │
│  Child starts with: role=PENDING, status=ANALYZING                           │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  8. PENDING AGENT: COMPLEXITY EVALUATION (agent_orchestrator.py:76)           │
│  ──────────────────────────────────────────────────────────────────           │
│  complexity_pipeline steps:                                                    │
│    1. ValidatePendingAgent                                                    │
│    2. BuildComplexityPrompt (Jinja2 template)                                 │
│    3. EmitPromptSent                                                          │
│    4. QueryLLM: "Is this task simple or complex?"                            │
│    5. EmitTokensConsumed                                                      │
│    6. ParseComplexityResult: {"complexity": "simple/complex"}                │
│    7. ApplyComplexityResult → agent.apply_complexity_result()                │
│                                                                               │
│  Events Emitted:                                                              │
│    • TokensConsumed                                                           │
│    • ComplexityEvaluated(complexity="simple", determined_role="WORKER")      │
│                                                                               │
│  Role Transition:                                                             │
│    • "simple" → WORKER (execute directly)                                    │
│    • "complex" → MANAGER (decompose further)                                 │
└───────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                           (Loop continues until all agents complete)
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  9. COMPLETION PROPAGATION (execution_service.py:351)                         │
│  ───────────────────────────────────────────────────                          │
│  When WORKER completes:                                                        │
│    1. WorkCompleted event persisted                                           │
│    2. _notify_parent_if_complete():                                           │
│       • Load parent agent                                                     │
│       • parent.handle_child_update(child_id, result)                         │
│       • Emit ChildCompleted event                                             │
│       • If all children done → parent emits WorkCompleted                    │
│    3. Propagates up the hierarchy until BOSS completes                       │
└───────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  10. FINAL RESULT (cli.py:123)                                                │
│  ────────────────────────────────────                                         │
│  _display_final_result(root_id):                                              │
│    1. get_agent_result(root_id) → load events, extract result                │
│    2. get_system_statistics() → count agents, events                         │
│    3. Print final result to console                                          │
│                                                                               │
│  All events persisted in PostgreSQL for later replay/viewing                 │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Agent Role Transitions

```
                    ┌─────────────────┐
                    │   AgentCreated  │
                    │   role=BOSS     │
                    └────────┬────────┘
                             │ assign_task()
                             ▼
                    ┌─────────────────┐
                    │ status=ANALYZING│
                    └────────┬────────┘
                             │ evaluate_task() → LLM decomposes
                             ▼
           ┌─────────────────────────────────────────┐
           │  SubtasksDefined + ChildSpawned(PENDING) │
           │  status=WAITING                          │
           └─────────────────────────────────────────┘
                             │
                             ▼
              ┌──────────────────────────┐
              │   Child: role=PENDING    │
              │   status=ANALYZING       │
              └────────────┬─────────────┘
                           │ evaluate_complexity() → LLM
                           ▼
          ┌────────────────┴────────────────┐
          │                                 │
          ▼                                 ▼
┌──────────────────┐              ┌──────────────────┐
│ ComplexityEvaluated│            │ ComplexityEvaluated│
│ complexity=simple│              │ complexity=complex│
│ role → WORKER    │              │ role → MANAGER   │
└────────┬─────────┘              └────────┬─────────┘
         │                                 │
         ▼                                 ▼
┌──────────────────┐              ┌──────────────────┐
│  execute_task()  │              │  evaluate_task() │
│  (claude code)   │              │  (recursive)     │
└────────┬─────────┘              └──────────────────┘
         │
         ▼
┌──────────────────┐
│  WorkCompleted   │
│  status=COMPLETED│
└──────────────────┘
         │
         ▼ (notify parent)
┌──────────────────┐
│  ChildCompleted  │
│  (on parent)     │
└──────────────────┘
```

---

## Key Components

| Layer | File | Responsibility |
|-------|------|----------------|
| Entry | `main.py` | Calls bootstrap, runs CLI |
| Bootstrap | `bootstrap/__init__.py` | Wires all dependencies |
| Presentation | `presentation/cli.py` | Click commands, user interaction |
| Application | `core/application/execution_service.py` | Orchestrates agent lifecycle |
| Application | `core/application/agent_orchestrator.py` | Delegates to pipelines |
| Application | `core/application/pipelines.py` | PipelineFactory creates 3 pipelines |
| Application | `core/application/pipeline/` | Composable step implementations |
| Domain | `core/domain/model.py` | AgentSession aggregate, event sourcing |
| Domain | `core/domain/events.py` | All domain events |
| Infrastructure | `infrastructure/adapters/claude_pty_adapter.py` | Spawns Claude Code CLI |
| Infrastructure | `infrastructure/adapters/postgres_event_store.py` | Event persistence |

---

## Domain Events

| Event | Emitted By | Purpose |
|-------|------------|---------|
| `AgentCreated` | `AgentSession.create()` | New agent initialized |
| `TaskAssigned` | `agent.assign_task()` | Task description assigned |
| `TokensConsumed` | `agent_orchestrator` | LLM cost tracking |
| `ComplexityEvaluated` | `evaluate_complexity()` | PENDING → WORKER/MANAGER |
| `SubtasksDefined` | `evaluate_task()` | Task decomposition result |
| `ChildSpawned` | `apply_subtasks_and_spawn_children()` | Child agent created |
| `StatusChanged` | Various | Agent status transitions |
| `CodeGenerationStarted` | `execute_task()` | Worker begins execution |
| `ThoughtCaptured` | `ClaudeCodePTYAdapter` | Worker thought stream |
| `WorkCompleted` | Worker/aggregation | Task finished successfully |
| `WorkFailed` | Error handling | Task failed with reason |
| `ChildCompleted` | `handle_child_update()` | Parent records child result |
