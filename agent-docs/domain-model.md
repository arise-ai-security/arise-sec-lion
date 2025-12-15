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
| `IDLE` | Created, no task assigned |
| `ANALYZING` | Processing task (evaluating, decomposing, or executing) |
| `WAITING` | Manager waiting for child results |
| `COMPLETED` | Successfully finished |
| `FAILED` | Encountered unrecoverable error |

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

Configuration: `bootstrap/infrastructure.py:34` - `worker_tool_type` setting

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
