# Domain Model - Agent Hierarchy

## Agent Roles

| Role | Purpose | Spawns Children? |
|------|---------|------------------|
| `BOSS` | Root agent, initiates decomposition | Yes → PENDING |
| `PENDING` | Evaluates task complexity | No (transforms to WORKER or MANAGER) |
| `MANAGER` | Decomposes complex tasks | Yes → PENDING |
| `WORKER` | Executes simple tasks via tools | No |

## Agent Statuses

| Status | Description |
|--------|-------------|
| `PENDING` | Created, no task assigned |
| `ANALYZING` | Processing task (evaluating, decomposing, or executing) |
| `IN_PROGRESS` | Actively working on task |
| `WAITING` | Manager waiting for child results |
| `COMPLETED` | Successfully finished |
| `FAILED` | Encountered unrecoverable error |
| `BLOCKED` | Waiting for external input |
| `TERMINATED` | Terminated early (sibling succeeded or budget depleted) |
| `VERIFYING` | Verification task in progress |

## Agent Lifecycle Diagram

```
                    ┌─────────────────────────────────────────┐
                    │              BOSS (root)                │
                    │  - Initiates task decomposition         │
                    │  - Only ONE in the system               │
                    └────────────────┬────────────────────────┘
                                     │ spawns children
                                     ▼
                    ┌─────────────────────────────────────────┐
                    │              PENDING                     │
                    │  - Evaluates task complexity via LLM    │
                    │  - Determines: SIMPLE or COMPLEX?       │
                    └────────────────┬────────────────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              │ SIMPLE                                      │ COMPLEX
              ▼                                             ▼
┌─────────────────────────┐               ┌─────────────────────────┐
│        WORKER           │               │        MANAGER          │
│  - Executes via tools   │               │  - Decomposes further   │
│  - Claude Code/OpenHands│               │  - Spawns PENDING       │
│  - Captures thinking    │               │  - Waits for children   │
└─────────────────────────┘               └─────────────────────────┘
```

## Key Domain Classes

### AgentSession (Aggregate Root)

Location: `core/domain/model.py:45`

| Method | Role(s) | Description |
|--------|---------|-------------|
| `create()` | All | Factory method, creates `AgentCreated` event |
| `assign_task()` | All | Assigns task, creates `TaskAssigned` event |
| `evaluate_complexity()` | PENDING | Evaluates via LLM, transforms to WORKER or MANAGER |
| `evaluate_task()` | BOSS, MANAGER | Decomposes task, spawns PENDING children |
| `execute_task()` | WORKER | Runs task via worker tool (Claude Code/OpenHands) |
| `handle_child_update()` | BOSS, MANAGER | Receives child completion notification |
| `fail_with_reason()` | All | Marks agent as failed |

### Subtask (Value Object)

Location: `core/domain/subtask.py:12`

Represents a decomposed piece of work with:
- `description`: What to do
- `priority`: Execution order hint
- `dependencies`: Other subtask IDs this depends on

### PromptBuilder (Domain Service)

Location: `core/domain/prompt_builder.py:18`

Composes hierarchical prompts from Jinja2 templates:
- System prompts (role identity)
- Strategy prompts (methodology)
- Task prompts (specific instructions)
- Output format prompts (JSON schemas)

## Worker Tools

Workers execute tasks using external tools that capture "thinking":

| Tool | Adapter | Use Case |
|------|---------|----------|
| Claude Code | `ClaudeCodePTYAdapter` | Anthropic-powered code generation |
| OpenHands | `OpenHandsAdapter` | Multi-provider LLM code generation |

Configuration: `bootstrap/infrastructure.py` - `worker_tool_type` setting

---

## Budget System

Agents manage budgets for resource allocation and cost control.

### Budget Flow

```
BOSS (initial_budget=1000)
  │
  ├─ allocates budget to subtask (333 per subordinate)
  │   ├─ Subordinate 1 (Claude) → succeeds → returns 111 * 1.2 = 133.2
  │   ├─ Subordinate 2 (Gemini) → terminated → returns 111 * 1.0 = 111
  │   └─ Subordinate 3 (GPT-4o) → terminated → returns 111 * 1.0 = 111
  │
  └─ BOSS recollects: 133.2 + 111 + 111 = 355.2
```

### Budget Events

| Event | Description |
|-------|-------------|
| `BudgetAllocated` | Initial budget assigned to agent |
| `BudgetAdjusted` | Budget modified (reward/penalty) |
| `BudgetRecollected` | Parent recollects budget from completed child |

### Reward/Penalty Ratios

| Outcome | Ratio | Example |
|---------|-------|---------|
| Success | 1.2x | 111 remaining → 133.2 returned |
| Failure | 0.8x | 111 remaining → 88.8 returned |
| Terminated | 1.0x | 111 remaining → 111 returned |

---

## Multi-Model Strategy

When a supervisor spawns subordinates, it creates 3 agents with different LLMs:

```
Subtask: "Open the text file"
  ├─ Subordinate 1: Claude Sonnet (method: vim)
  ├─ Subordinate 2: Gemini Pro (method: nano)
  └─ Subordinate 3: GPT-4o (method: cat)
```

**First success wins** - other subordinates are immediately terminated.

### Events

| Event | Description |
|-------|-------------|
| `SubordinatesSpawned` | Multiple subordinates created for same subtask |
| `FirstSuccessRecorded` | First subordinate succeeded, siblings terminated |
| `AllSubordinatesFailed` | All subordinates failed, retry decision needed |

---

## Task Queue

Each agent maintains a FIFO queue of subtasks:

| Event | Description |
|-------|-------------|
| `TaskEnqueued` | Subtask added to queue |
| `TaskDequeued` | Subtask removed for processing |
| `SubtaskRetried` | Failed subtask re-inserted with revised context |

---

## Verification System

Supervisors can inject verification tasks based on heuristics:

1. **Complexity-based**: Large subtree + complex task
2. **Random probability**: 10% chance per completion
3. **Suspicious reports**: Edits don't match task complexity
4. **Time-based**: Too long since last verification

### Events

| Event | Description |
|-------|-------------|
| `VerificationInjected` | Verification task added to queue |
| `VerifierSpawned` | Independent verifier agent created |
| `VerificationCompleted` | Verifier finished, pass/fail result |
| `TaskReinjected` | Failed verification triggers task redo |

---

## Execution Service (The Brain)

Location: `core/application/execution_service.py:25`

Orchestrates the entire system:
1. Loads aggregate from event store
2. Dispatches to domain method based on role/status
3. Persists events with OCC
4. Handles child spawning and parent notification
5. Runs the main system loop

### Dispatch Logic

```
if status == ANALYZING:
    if role == PENDING    → evaluate_complexity()
    if role == BOSS/MGR   → evaluate_task()
    if role == WORKER     → execute_task()
```

See: `core/application/execution_service.py:181` - `_dispatch_agent_action()`
