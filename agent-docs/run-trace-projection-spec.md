# Run Trace Projection — Implementation Spec

## Purpose

Build a read-only trace projection over the existing event store that produces a canonical JSON tree summarizing a complete agent run. The trace is a CQRS read model — not a new tracing subsystem. It reconstructs the run tree from existing domain events in a single query.

**Use cases:**
- Debug a failed run by inspecting the full execution tree at a glance
- Pipe the JSON trace to an LLM for automated root-cause analysis
- Lay groundwork for future cross-run analytics (not in scope here)

**Layer:** `core/query/projections/` (projection + schema), `query/api/routes/` (endpoint), `presentation/` (text renderer)
**Category:** Topological/query — NOT cybersecurity.

---

## 0. Pre-Flight

Read these files before writing any code:

```
.claude/docs/conventions.md        — naming, imports, error handling
.claude/docs/patterns.md           — how projections are built
.claude/docs/architecture.md       — layer boundaries
```

Existing code to study (building blocks, not rewrite targets):

| File | Why |
|---|---|
| `core/query/projections/models.py` | Existing read models, follow same patterns |
| `core/query/projections/impl/summary.py` | `SummaryProjection` — single-pass extraction pattern |
| `core/query/projections/impl/agent_list.py` | `AgentListProjection` — lightweight projection pattern |
| `core/query/projections/hierarchy_builder.py` | Tree construction from flat agent map |
| `core/ports/event_store_port.py` | `EventStoreReadPort` interface |
| `core/domain/events/events.py` | All 31 event definitions |
| `query/api/routes/agents.py` | Existing endpoint patterns |
| `query/api/routes/events.py` | Event categorization reference |
| `presentation/` | Existing renderer patterns |

---

## 1. Schema — Frozen Pydantic Models

Define in `core/query/projections/models.py` (or a new file if `models.py` is already large — check first).

### TraceEventRefs

```
aggregate_id:       UUID
first_seq:          int              # min(sequence_number), NOT hardcoded 0
last_seq:           int              # max(sequence_number)
event_count:        int
```

### TraceNode

```
agent_id:           UUID
parent_id:          UUID | None
role:               str              # AgentRole value
status:             str              # AgentStatus value
sibling_index:      int
depends_on:         list[int]        # sibling indices this node waited on
task:               str              # from TaskAssigned.task_description
model:              str | None       # primary model used (see derivation §2)
worker_tool:        str | None       # first tool seen: "openhands" | "claude_sdk" | "google_adk" | None

start_time:         datetime | None
end_time:           datetime | None
duration_s:         float | None

tokens_prompt:      int
tokens_completion:  int
cost_usd:           float

decision_summary:   str | None       # deterministic — see §3
result:             str | None       # from WorkCompleted.result (truncated)
result_truncated:   bool             # true if result was truncated
error:              str | None       # from WorkFailed.reason
status_reason:      str | None       # from terminal StatusChanged.reason
failure_reason:     str | None       # see §2 for exact derivation rule

retry_count:        int              # count of RetryScheduled events for this agent
last_output_excerpt: str | None      # see §2 — NOT a literal command, an excerpt
last_output_excerpt_truncated: bool  # true if excerpt was truncated

on_failure_path:    bool             # true if this node or any descendant failed
children:           tuple[TraceNode, ...]  # tuple, not list — shallow freeze is real freeze

event_refs:         TraceEventRefs   # pointer back to raw events
```

### RunTrace

```
trace_id:           UUID             # same as root agent_id
task:               str              # root task description
status:             str              # root agent's terminal status
domain_metadata:    dict | None      # from RunStarted.domain_metadata (e.g. CVE info)
start_time:         datetime | None  # from RunStarted.occurred_at
end_time:           datetime | None  # from RunCompleted.occurred_at
total_duration_s:   float | None     # from RunCompleted.duration_seconds
total_agents:       int
total_tokens:       int
total_cost_usd:     float
root:               TraceNode
```

All models: `model_config = {"frozen": True}`. Use `tuple` for `children` to ensure deep immutability.

---

## 2. Field Derivation — Exact Source Events

For each agent, replay its events (pre-sorted by `sequence_number` from the event store). Single pass per agent.

### Identity & Structure

| Field | Source |
|---|---|
| `agent_id` | `AgentCreated.aggregate_id` |
| `parent_id` | `AgentCreated.parent_id` |
| `role` | `ComplexityEvaluated.determined_role` if present, else `AgentCreated.role` |
| `status` | Last `StatusChanged.new_status` |
| `sibling_index` | `AgentCreated.sibling_index` |
| `depends_on` | From parent's **latest** `SubtasksDefined` emitted **before** this child's `AgentCreated.occurred_at`. Match by `sibling_index`. Default `[]` if unavailable. |
| `task` | `TaskAssigned.task_description` |
| `model` | First `TokensConsumed.model`, fallback `WorkerCostRecorded.model`, fallback `AgentCreated.config` model field |
| `worker_tool` | First `CodeGenerationStarted.tool_name` (workers only, `None` for managers/boss) |

### Timing

| Field | Source |
|---|---|
| `start_time` | `AgentExecutionStarted.occurred_at` (fallback: `AgentCreated.occurred_at`) |
| `end_time` | `AgentExecutionFinished.occurred_at` (fallback: last event's `occurred_at`) |
| `duration_s` | `AgentExecutionFinished.duration_seconds` (fallback: `end_time - start_time` if both present) |

### Tokens & Cost — De-duplication Rule

**`TokensConsumed` takes precedence.** Use `WorkerCostRecorded` only when no `TokensConsumed` events exist for that agent. These two event types may overlap for the same LLM calls; summing both would double-count.

| Field | Source |
|---|---|
| `tokens_prompt` | Sum of `TokensConsumed.prompt_tokens` if any exist; else sum of `WorkerCostRecorded.prompt_tokens` |
| `tokens_completion` | Sum of `TokensConsumed.completion_tokens` if any exist; else sum of `WorkerCostRecorded.completion_tokens` |
| `cost_usd` | Sum of `TokensConsumed.cost_usd` if any exist; else sum of `WorkerCostRecorded.cost_usd` |

### Results & Errors

| Field | Source |
|---|---|
| `result` | `WorkCompleted.result`, truncated per `max_result_length` param |
| `result_truncated` | `true` if original exceeded `max_result_length` |
| `error` | `WorkFailed.reason` |
| `status_reason` | `StatusChanged.reason` from the event where `new_status` is terminal (`COMPLETED`, `FAILED`, `SKIPPED`) |

### Failure Reason — Deterministic Rule

Single rule: use this node's own `WorkFailed.reason`. If this node has no `WorkFailed` but is on the failure path (a manager with failed children), walk descendants depth-first and take the **deepest** failed node's `WorkFailed.reason`, tie-broken by **earliest** `occurred_at`. This is a string field, not an enum — no taxonomy.

### Retries & Last Output

| Field | Source |
|---|---|
| `retry_count` | Count of `RetryScheduled` events for this agent |
| `last_output_excerpt` | Scan `ThoughtCaptured` events in reverse. Take the last one where `output_type == "output"`. Strip ANSI escape codes. Collapse consecutive whitespace. Prefer the **tail** (last N chars) for terminal output — the end of a failed command run is more diagnostic than the beginning. Truncate per `max_excerpt_length` param. |
| `last_output_excerpt_truncated` | `true` if original exceeded `max_excerpt_length` |

### Computed After Tree Assembly

| Field | Source |
|---|---|
| `on_failure_path` | Bottom-up: `true` if this node's status is `FAILED`, or any child has `on_failure_path == true` |
| `children` | Populated during tree assembly, sorted by `sibling_index` |
| `event_refs` | `{ aggregate_id, first_seq: min(seq), last_seq: max(seq), event_count: len }` |

---

## 3. Decision Summary — Deterministic, No LLM

This field answers: "what did this agent decide to do?" Mechanically derived. Never calls an LLM. If the data isn't in the events, the field is `null`.

### BOSS / MANAGER

```
If SubtasksDefined exists:
  "Decomposed into {n} subtasks: {subtask[0].description[:80]}, {subtask[1].description[:80]}, ..."
  Truncate entire string to 300 chars.
If RedecompositionTriggered exists:
  Append " (redecomposed: {reason[:100]})"
If DecisionInfeasible exists:
  "Deemed infeasible: {reason[:200]}"
```

### WORKER

Describe what was **attempted**, not the outcome (outcome is in `result`/`error`):

```
If CodeGenerationStarted exists:
  "Executed via {tool_name}"
If neither CodeGenerationStarted nor any work events:
  null
```

Do NOT repeat `result` or `error` in `decision_summary` for workers. The trace has separate fields for those. Duplication wastes context budget when fed to an LLM.

### PENDING (not yet evaluated)

`null`

---

## 4. Projection Implementation

**File:** `core/query/projections/impl/trace_projection.py`

**Class:** `TraceProjection`

**Input:** result of `EventStoreReadPort.get_hierarchy_events_grouped(root_id)` — returns `dict[UUID, list[DomainEvent]]`, all events for the entire subtree, grouped by `aggregate_id`, fetched in ONE recursive CTE query.

**Algorithm:**

1. **Extract per-agent data.** For each `aggregate_id`, single-pass its event list to extract all fields from §2 into a flat intermediate dict keyed by `agent_id`. Also collect all `SubtasksDefined` events keyed by `(parent_id, occurred_at)` for `depends_on` resolution.

2. **Resolve `depends_on`.** For each agent with a `parent_id`, find the parent's latest `SubtasksDefined` emitted before this agent's `AgentCreated.occurred_at`. Match by `sibling_index` to get `depends_on`.

3. **Build parent-to-children map** using `parent_id` from `AgentCreated`.

4. **Assemble tree recursively** from root. Sort children by `sibling_index`. Convert children lists to tuples.

5. **Compute `on_failure_path`** bottom-up: post-order traversal, mark any node whose status is `FAILED` or that has a child with `on_failure_path == true`.

6. **Compute `failure_reason` propagation.** For manager nodes without their own `WorkFailed`, walk failed descendants depth-first, take the deepest `WorkFailed.reason`, tie-break by earliest `occurred_at`.

7. **Compute `RunTrace`-level aggregates** (`total_agents`, `total_tokens`, `total_cost_usd`) by summing across all nodes.

8. Return `RunTrace`.

**Constraints:**
- Stateless. No side effects. Pure function from events to `RunTrace`.
- No database calls inside the projection.
- No LLM calls.
- Handle partial/in-progress runs: missing fields become `None`, not errors.

---

## 5. API Endpoint

**File:** `query/api/routes/agents.py` (add to existing router) OR `query/api/routes/trace.py` (new router if `agents.py` is already dense — check first)

```
GET /api/agents/{root_id}/trace
```

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `max_result_length` | int | 500 | Truncation limit for `result` fields |
| `max_excerpt_length` | int | 200 | Truncation limit for `last_output_excerpt` |
| `max_error_length` | int | 1000 | Truncation limit for `error`/`failure_reason` (less aggressive) |
| `include_pending` | bool | true | Include nodes still in PENDING status |

**Response:** `RunTrace` serialized as JSON.

Follow existing endpoint patterns:
- Get `event_store` from dependency injection
- Call `get_hierarchy_events_grouped(root_id)`
- Pass to `TraceProjection.build(events, ...)`
- Return result

---

## 6. Text Renderer — Failure-Biased

**File:** `presentation/renderers/trace_renderer.py`

**Function:** `render_trace_text(trace: RunTrace) -> str`

Takes a `RunTrace` and produces indented text. The text is a RENDERING of the JSON schema, not a separate data model.

### Header

```
RUN: "{task}" | {STATUS} | {total_duration_s}s | ${total_cost_usd} | {total_agents} agents
```

### Per-Node Format

```
{status_icon} {ROLE}[{status}] "{task}" ({duration_s}s, ${cost_usd})
```

**Status icons:** `+` completed, `x` failed, `~` in_progress, `o` pending, `-` skipped

**Failure path suffix:** nodes with `on_failure_path == true` get ` << FAILURE PATH` appended.

### Selective Detail Rendering

**Always show** (for every node): status line above.

**Show only for nodes that are failed, retried (`retry_count > 0`), or on the failure path:**

```
    Decision: {decision_summary}
    Result: {result}
    Error: {error}
    Retries: {retry_count}
    Last output: {last_output_excerpt}
```

Omit any of these sub-lines if the value is `null` or zero.

**Successful nodes NOT on the failure path:** status line only. No detail fields. This is the main noise reduction — successful subtrees collapse to one line each.

### Tree Connectors

Use `|--`, `\--`, `|  ` for visual tree structure (ASCII-safe, no Unicode box-drawing).

---

## 7. CLI Command (Lower Priority)

If a Click CLI group exists in `presentation/` or `bootstrap/`, add a `trace` subcommand:

```
python main.py trace <root_id> [--json] [--text]
```

Default: `--text`. If `--json`, output raw JSON to stdout (pipeable to `claude`, `jq`, etc.). The CLI calls the same projection and renderer. No separate logic.

---

## 8. Constraints

- **ONE database query:** `get_hierarchy_events_grouped(root_id)`. No N+1.
- **Projection is a pure function:** events in, `RunTrace` out. No DB calls inside.
- **No LLM calls.** Every field is mechanically derived from event data.
- **All new models frozen** (`model_config = {"frozen": True}`). Use `tuple` for `children`.
- **Follow existing import conventions:** `core/` never imports `infrastructure/`.
- **Truncation limits configurable** via function params, not hardcoded magic numbers.
- **Less aggressive truncation for error fields** — `error`, `failure_reason`, `status_reason` get a higher default limit (1000 chars) than `result` (500) or `last_output_excerpt` (200). These are the highest-signal fields for debugging.
- **`last_output_excerpt` preprocessing:** strip ANSI escape codes, collapse consecutive whitespace, prefer tail (last N chars).
- **Handle partial runs gracefully:** if a run is in progress, the trace renders with whatever data exists. Missing fields become `None`, not errors.

---

## 9. Do NOT

- Add a new event type. This is read-side only.
- Modify the event store schema or write path.
- Add LLM-based summarization or classification.
- Build critical-path computation. Include raw timing data; consumers compute it.
- Add a failure taxonomy enum. The trace is descriptive.
- Create a new database table or materialized view.
- Add a dashboard tab. API + CLI only for v1.
- Modify existing projections or endpoints.
- Sum both `TokensConsumed` and `WorkerCostRecorded` — de-duplicate per §2.
- Repeat `result`/`error` content inside `decision_summary` for workers.
