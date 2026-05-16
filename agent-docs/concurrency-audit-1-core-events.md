# Concurrency Audit (Agent 1 of 5): Core Domain + Event Sourcing

Scope: `core/`, `infrastructure/adapters/postgres_event_store.py`,
`infrastructure/sql/create_events_table.sql`, `bootstrap/composition.py`,
`bootstrap/bootstrap.py`. All findings are read-only observations; no code changed.

---

## Q1. Aggregate identity & event emission within ONE run

**Finding:** There is exactly **one logical `AgentSession` aggregate per agent_id**
(BOSS, every MANAGER, every WORKER). However, the in-memory `AgentSession`
*instance* is **per-coroutine, not shared**: `run_agent_step` always re-loads
the aggregate from the event store at the top of every step.

`core/application/execution_service.py:259-271`:

```python
async def run_agent_step(self, agent_id: UUID) -> None:
    """Execute one workflow step: load -> dispatch -> persist with OCC retry."""
    retry_count = 0
    while retry_count < self._config.max_retries:
        try:
            agent = await self._load_agent_with_context(agent_id)
            current_version = agent.version
            await self._dispatch_agent_action(agent)
            uncommitted = await self._persist_agent_events(agent, current_version)
            await self._handle_post_step(agent, uncommitted)
            return
```

Mutable state on the aggregate (`_changes`, `_sequence`, `child_ids`,
`child_reports`, `failed_children`, `version`) lives on the per-step instance
only. There is **no shared mutable aggregate object** that two manager
coroutines could trample. `_changes` and `_sequence` are private lists/ints
on a freshly loaded object (agent_session.py:449-487).

The serialization boundary is the database: two coroutines that load the
same agent (e.g., parent notifications from two children completing
near-simultaneously) each get their own `AgentSession` instance and append
events through `EventStorePort`. The `UNIQUE(aggregate_id, sequence_number)`
constraint is the only inter-coroutine ordering primitive.

**Severity:** Not a bug. **P3 / no concern.**

**Fix:** None needed. (Documenting the invariant in `core-domain.md` —
"never share an `AgentSession` instance across coroutines" — would be a
defensive comment, not a fix.)

---

## Q2. OCC sequence-number races

**Finding:** Sequence numbers are assigned per-instance: `_next_sequence`
increments `self._sequence` (agent_session.py:543-545):

```python
def _next_sequence(self) -> int:
    self._sequence += 1
    return self._sequence
```

Two coroutines holding two separately loaded `AgentSession` objects for the
same `agent_id` will both compute the same `_sequence + 1` if they loaded at
the same version. They both fill in the next sequence_number, both call
`event_store.append(...)`, the second hits
`asyncpg.UniqueViolationError`, the store wraps it as
`ConcurrencyError`, and `run_agent_step`'s outer `except ConcurrencyError`
loop retries by reloading and re-emitting (execution_service.py:273-280).

The OCC mechanism is **purely the UNIQUE constraint** — `expected_version`
is accepted as a parameter on `PostgresEventStore.append/append_batch` but
**never used in the SQL** (postgres_event_store.py:162-204, 206-264). It is
only echoed back into the `ConcurrencyError`'s diagnostic field. This is
misleading API surface but not a correctness bug, because the database
constraint enforces the invariant the parameter pretends to.

What CAN go wrong: a `ConcurrencyError` raised mid-batch leaves no events
persisted (the transaction in `append_batch` is atomic — see line 227,
`async with self.pool.acquire() as conn, conn.transaction()`), so no torn
writes. The retry loads, re-emits, and re-appends with the new
sequence_number range.

**Severity:** **P2 (performance only)** — every OCC conflict costs one
extra LLM-or-worker round trip's worth of work, plus a DB round trip to
reload. With 10 parallel workers under one BOSS, conflicts are localized
to the parent (BOSS) on `ChildCompleted`/`WorkCompleted`, which are cheap
to retry. **EXCEPT** see Q-X (parent-notifier finding) which converts
some retries into permanent hangs.

**Fix:**
- Tighten the API: drop `expected_version` from `append`/`append_batch` or
  add `WHERE NOT EXISTS (SELECT 1 FROM events WHERE aggregate_id = $2 AND
  sequence_number > $version_param)` to actually use it. Today, accepting
  the param without using it invites callers to assume version-level
  guards exist.
- Document that the only guard is `(aggregate_id, sequence_number)` PK.

---

## Q3. Replay/load races between query side and orchestrator

**Finding:** Both sides read `events` via the same `get_events(aggregate_id)`
query (postgres_event_store.py:266-314), which is a single-statement SELECT
ordered by `sequence_number ASC`. PostgreSQL gives that statement
**read-committed snapshot consistency by default**: no torn reads — the
reader sees either the events that have committed or it does not, never a
partial batch.

Crucially, `append_batch` wraps multiple inserts in a single transaction
(postgres_event_store.py:227):

```python
async with self.pool.acquire() as conn, conn.transaction():
    await conn.executemany(...)
```

So a concurrent `get_events` either sees the entire batch or none of it.
For single-event `append` (line 172), there is no batch atomicity needed
— each insert is its own statement.

The query-side `AgentQueryService` (`get_active_agent_ids`,
`has_non_terminal_agents`) builds a `dict[UUID, list[DomainEvent]]` from
`get_hierarchy_events_grouped`, which is a single recursive CTE
(postgres_event_store.py:482-499). The grouping is computed in one round
trip, so all events seen are mutually consistent.

The one nit: events for *different* aggregates in `get_hierarchy_events_grouped`
are read in one snapshot, but a child's `AgentCreated` may be visible
without the parent's `ChildSpawned` if the parent's transaction commits
*after* the recursive CTE evaluates — actually impossible because
`save_new_agent` for a child is called *after* the parent's
`persist_events` (execution_service.py:879-883 in `_handle_post_step`).
So if the child's `AgentCreated` is visible, the parent's `ChildSpawned`
is necessarily visible too.

**Severity:** **P3 / no real concern.**

**Fix:** None needed.

---

## Q4. Optimistic locking on parent state from children (PARENT-NOTIFIER HANG)

**P1 Finding — primary concurrency risk in this scope.**

The recursive `notify_if_complete` chain in `parent_notifier.py:40-89` is
**not OCC-retry-safe** because the outer `run_agent_step` retry reloads
the leaf agent, but on retry the *parent* can no longer accept a child
event because it raced to `COMPLETED`. The propagation chain breaks
silently.

### Trace

Setup:
- BOSS Z has children X, X' (managers).
- X has worker child Y; X' has worker child W.
- Both Y and W finish near-simultaneously in the same event loop.

Step 1: `run_agent_step(Y)` finishes worker execution. In
`_handle_post_step` (execution_service.py:888-889), the orchestrator calls
`parent_notifier.notify_if_complete(Y)`:

`parent_notifier.py:54-89`:

```python
if child.status != AgentStatus.COMPLETED or child.parent_id is None:
    return
try:
    parent = await self._repository.load(child.parent_id)
except AgentNotFoundError as e:
    raise ValueError(...) from e
if parent.status != AgentStatus.WAITING:
    return            #  <-- the silent-drop branch (line 67)

parent_version = parent.version
report = child.build_report()
parent.handle_child_update(...)
await self._repository.persist_events(parent, parent_version, ...)

if parent.status == AgentStatus.COMPLETED:
    await self.notify_if_complete(parent)    # recurse upward
```

`notify_if_complete(Y)` loads X (WAITING, has only one child Y), emits
`ChildCompleted` then `WorkCompleted` (because all children reported —
agent_session.py:170-186). X is now COMPLETED. Recurse: load Z (WAITING),
emit `ChildCompleted(child=X)` — sequence number e.g. 47.

Step 2: Concurrently, `notify_if_complete(W)` from another coroutine
already loaded Z at the same version. It emits `ChildCompleted(child=X')`
— also computes sequence number 47. Whichever `append` reaches the DB
first wins. Suppose W's wins.

Step 3: Y-coroutine's `persist_events` for Z raises `ConcurrencyError`
(postgres_event_store.py:194-198). The exception bubbles up through
`notify_if_complete(X)` -> `notify_if_complete(Y)` -> `_handle_post_step`
-> `run_agent_step` -> outer `except ConcurrencyError` (execution_service.py:273).

Step 4: Retry. `run_agent_step` reloads **Y, not Z**. Y is still
COMPLETED (its events were persisted in Step 1). `_dispatch_agent_action`
checks `agent.status != AgentStatus.ANALYZING` and returns no-op
(execution_service.py:851-854). `_persist_agent_events` returns empty (no
new events). `_handle_post_step` runs: `agent.status == COMPLETED` →
calls `notify_if_complete(Y)` again.

Step 5: `notify_if_complete(Y)` loads X — X is now **COMPLETED**, not
WAITING. Line 66-67 returns silently. No further upward propagation.

Step 6: Z's `ChildCompleted(child=X)` is **never emitted**. Z stays
WAITING. The system loop's `get_active_agent_ids` no longer schedules Z
(WAITING agents are filtered out — query_service.py:272-276). Z sits
forever until `max_run_duration_seconds` fires, at which point the run
times out and is recorded as `failed`/`timed_out` even though all leaf
work succeeded.

### Same defect in `notify_if_failed`

The matching check at `parent_notifier.py:106-107`:

```python
if parent.status != AgentStatus.WAITING:
    return
```

drops failure propagation under the same race. Two siblings failing
simultaneously: the second one's failure will land at a parent that is
already FAILED (from the first sibling's path), and the chain will not
walk up to the grandparent.

### Severity

**P1.** The run does not corrupt data — events that did persist are
correct — but the run **hangs to its global deadline**, then fails. From
the user's perspective: a run with all children COMPLETED is reported as
`timed_out` / `failed`. This is harder to diagnose than a crash because
the error is far removed in time from the cause.

Probability rises with: number of siblings (more contention on parent
sequence), degree of parallelism within one run, and prevalence of
near-simultaneous worker completion (likely with the new parallel manager
support).

### Recommended fix

- In `notify_if_complete`, when `parent.status != AgentStatus.WAITING`:
  - If parent is **COMPLETED** (meaning a sibling already drove it to
    completion and the current child's report was effectively absorbed
    by that completion path), do not return — instead **recurse upward**
    to give the grandparent a chance to receive a notification it might
    not have received.
  - If parent is **FAILED**, return is OK (failure has already
    propagated upward via the other path).
- In `notify_if_failed`, mirror the change.
- Better still: rather than re-deriving who-needs-notification from a
  per-coroutine in-memory view of parent state, drive the upward
  propagation **off the events themselves**. A notification step keyed
  on "which parents have not yet observed all their children's terminal
  events" is robust to OCC retry loss because events are durable.

---

## Q5. Aggregate ID collisions across runs

**Finding:** All aggregate IDs are `uuid4()`:

- `root_id = uuid4()` for BOSS in `execution_service.py:230` (`AgentExecutionService.create_boss_agent`).
- `child_id = uuid4()` per spawned child in `agent_session.py:748`
  (`_emit_child_spawns`).
- `event_id: UUID = Field(default_factory=uuid4)` on `DomainEvent`
  (events.py:40).
- `SharedStore` aggregate_id derived from `root_id` via
  `shared_context_aggregate_id(root_id)` — so it inherits root's UUID
  randomness (shared_context_adapter.py:55).

10 parallel runs in 10 subprocesses: probability of a v4 collision is
~10^-37 per pair. Not a real concern.

**Severity:** **P3 / no concern.**

**Fix:** None.

---

## Q6. Connection-pool sizing

**Finding:** `PostgresEventStore.connect()` uses `asyncpg.create_pool` with
**no explicit min/max** (postgres_event_store.py:139-143):

```python
self.pool = await asyncpg.create_pool(
    self.connection_string,
    init=init_connection,
    statement_cache_size=0,
)
```

`asyncpg.create_pool` defaults to `min_size=10, max_size=10` per process.
With 10 parallel runs, that is 100 connections total. Postgres
`max_connections=300` handles this comfortably.

The `SharedContextPort` adapter shares the same `event_store` instance
(`shared_context_adapter.py:33-39`), so it pulls connections from the
same pool — no extra footprint per-process.

The pool acquires-and-releases a connection per call (`async with
self.pool.acquire() as conn`). Many concurrent coroutines will queue at
the pool semaphore once 10 are in flight; with batch appends taking
~1-5 ms, this is not a bottleneck for the event store itself. However,
**`get_hierarchy_events_grouped` is called repeatedly from
`get_active_agent_ids` and `has_non_terminal_agents` on every poll
iteration** (execution_service.py:315, 331-335), and that recursive CTE
holds a connection for the duration of a potentially heavy scan. Under 10
parallel runs all polling simultaneously, this can starve the pool of
connections momentarily and slow down the dispatcher loop.

**No code opens connections outside the pool** in this scope. (`pyright
no-member` grep returns only the one `create_pool` call.)

**Severity:** **P2 (performance only).** Default pool is fine for
correctness; under stress the recursive CTE on every poll iteration is
likely the actual bottleneck.

**Fix:**
- Make pool sizes configurable from `Settings` (e.g.,
  `database.pool_min`, `database.pool_max`).
- Cache the result of `get_hierarchy_events_grouped` for the duration of
  a single poll iteration (already inferred from
  `get_active_agent_ids` calling it once). The bigger issue is *between*
  poll iterations every `poll_interval` seconds. Consider an
  incremental query (`get_events_batch_incremental` exists for exactly
  this — postgres_event_store.py:688-750 — but the system-loop poll
  doesn't use it).

---

## Q7. Implicit locks / SELECT FOR UPDATE / advisory locks

**Finding:** None. Grepping for `FOR UPDATE`, `pg_advisory`, or
`advisory_lock` across `core/`, `infrastructure/`, `bootstrap/`, and SQL
files returns nothing. The schema (`create_events_table.sql`) has no
single-row metadata table, no global counter table — only the events
table with the unique constraint and a few read indexes.

**No cross-run serialization point exists in this scope.** Two runs
appending to different aggregates (different `aggregate_id`) do so
fully independently in Postgres. The unique constraint is per-row so it
contends only when *two writes target the same `(aggregate_id,
sequence_number)`*, which by Q5 is essentially zero across runs.

**Severity:** **P3 / no concern.**

**Fix:** None.

---

## Q8. Snapshot or projection state across runs in the same process

**Finding:** Each run is its own subprocess (per the experiment runner
contract). Within a single subprocess, only one BOSS runs at a time —
the CLI invokes `cli.run_task(task, ...)` once and exits.

That said, there ARE in-memory caches in the orchestrator:

- `HierarchyLimitsRegistry._limits: dict[UUID, HierarchyLimits]` —
  populated by `create_root` and `propagate_to_child`
  (`hierarchy_limits_registry.py:11-54`). Per-process state, but
  `_reset_for_new_run` (execution_service.py:715-731) calls
  `self._limits_registry.reset()`. Safe per run.
- `ChildAgentFactory._total_created: int` — incremented in
  `create_children_from_events` (`child_factory.py:199`) and
  `create_from_event` (`:168`). Also reset by
  `_reset_for_new_run` via `self._child_factory.reset(initial_count=1)`.
  Safe per run.
- `RetryPolicy` state — reset by `self._retry_policy.reset()`.

If `bootstrap` were ever to drive multiple BOSS aggregates in one
subprocess (it does not today), all three of the above would silently
mix runs. Today the CLI exits after one run, so it is moot.

**Inside one run, multiple manager coroutines DO share these registries**:

- `HierarchyLimitsRegistry` is read concurrently by
  `_load_agent_with_context` (execution_service.py:837-849) — pure dict
  reads, safe under GIL.
- `ChildAgentFactory._total_created` is read by
  `_check_limit_violations` (agent_orchestrator.py:620-624) and written
  by `create_children_from_events` (child_factory.py:199). See Q-extra
  below — there is a **TOCTOU** here.

**Severity for cross-run leakage:** **P3** — only matters if the
subprocess model changes.

**Fix:** Document the invariant ("one BOSS per subprocess") in
`bootstrap.py`. If the subprocess assumption ever weakens, scope these
registries per-run (key by `root_id`) rather than per-process.

---

## Q9. Time/clock-based fields in events

**Finding:** Every event has `occurred_at: datetime = Field(default_factory=_utc_now)`
(events.py:43-44; `_utc_now()` returns `datetime.now(UTC)`). Resolution
is microseconds on modern Python; ties are possible when two events are
emitted in the same Python expression chain — e.g.,
`handle_child_update` emits `ChildCompleted` then `WorkCompleted` in
adjacent statements (agent_session.py:161-187). On a fast CPU, both
calls to `datetime.now(UTC)` can land in the same microsecond.

For **replay** (`get_events` ordered by `sequence_number ASC`), this is
fine — `sequence_number` is a strict total order per aggregate.

For **projection / display**, two CTEs in `get_boss_agent_summaries`
order by `occurred_at DESC` only, with no `sequence_number` tiebreaker:

`postgres_event_store.py:626-631`:

```sql
FROM events e
INNER JOIN boss_agents b ON e.aggregate_id = b.agent_id
WHERE e.event_type IN (
    'StatusChanged', 'WorkCompleted', 'WorkFailed',
    'VerificationFailed', 'DecisionInfeasible',
    'RetryScheduled', 'RedecompositionTriggered'
)
ORDER BY e.aggregate_id, e.occurred_at DESC
```

If `WorkCompleted` and `RetryScheduled` (or `WorkCompleted` and
`StatusChanged`) share `occurred_at` exactly, the listing's "latest
status" can flip on every query, showing the BOSS as either
`completed` or `analyzing` non-deterministically. This is a **display
artifact, not a correctness bug** — the underlying event log is correct.

The same lack of secondary sort affects `task_descriptions` and
`run_info` CTEs (`:610, :650`), but each agent has only one
`TaskAssigned` and one `RunStarted`, so ties cannot exist in practice.

**Severity:** **P2 (display flicker only).** Replay correctness is
unaffected because `get_events` orders by `sequence_number ASC`.

**Fix:** Add `sequence_number DESC` as the secondary sort key in the
`latest_status` CTE — and `boss_agents` as well, since two BOSS
`AgentCreated` events from the *same* runner spawning two runs in the
same subprocess could share `occurred_at` (rare today, but free
defense):

```sql
ORDER BY e.aggregate_id, e.occurred_at DESC, e.sequence_number DESC
```

---

## Q-extra. ChildAgentFactory limit-check TOCTOU

**Finding:** `_check_limit_violations` reads `child_factory.total_created`
and decides whether to spawn (agent_orchestrator.py:620-624):

```python
max_total = self._child_factory.max_total_agents
if max_total > 0:
    current_total = self._child_factory.total_created
    remaining = max_total - current_total
    if len(subtasks) > remaining:
        violations.append({...})
```

The actual increment happens in `child_factory.create_children_from_events`
(child_factory.py:199), **after `asyncio.gather`**:

```python
results = await asyncio.gather(
    *[self._create_child_parallel(event, parent_id) for event in events]
)
# Update counter after all parallel operations complete
# This avoids race conditions on _total_created
self._total_created += len(events)
```

The comment "avoids race conditions on _total_created" is true *between
the per-event tasks of one gather*. But it does **not** guard the
case where two `evaluate_task` coroutines run concurrently on
two different MANAGER agents:

- Both decompose, both call `_check_limit_violations` at the same
  `total_created = N`.
- Both decide `len(subtasks) <= max_total - N`.
- Both spawn. `_total_created` becomes `N + count_1 + count_2`,
  exceeding `max_total`.

**Severity:** **P1** when `max_total_agents` is set (it is — see
`OrchestrationConfig.max_total_agents`) and parallel decomposition is
active. The system silently exceeds the configured cap. Today's runs
appear to be hierarchical with one BOSS plus a few managers, so the
window is narrow but real once 5+ managers run in parallel.

**Fix:**
- Replace the read+spawn pair with a single atomic
  `try_reserve(N) -> bool` on `ChildAgentFactory`. Reserve the slots
  before `apply_subtasks_and_spawn_children` and release the
  reservation if the spawn fails downstream. (Counts are not known
  until the LLM decomposition returns, so reservation must be done
  after parsing the subtasks but before emitting child events.)
- Or move the cap check to the database (e.g., a count query on
  `events WHERE event_type='AgentCreated' AND root_id=X`) wrapped in a
  short transaction.

---

## Shared vs. not-shared table (audit scope)

| Resource | Shared across runs (10 procs)? | Shared across managers (one proc)? | Notes |
|---|---|---|---|
| `events` table (Postgres) | **SHARED** | **SHARED** | One table, all writes go through `(aggregate_id, sequence_number)` PK |
| `asyncpg` connection pool | NOT (per-process) | SHARED (per-process pool of ~10 conns) | Default `max_size=10`. No explicit sizing |
| `AgentSession` in-memory state | NOT shared (per subprocess) | NOT shared (each `run_agent_step` reloads from DB) | Per-coroutine instance; not a shared mutable object |
| `HierarchyLimitsRegistry._limits` dict | NOT shared (per process) | SHARED (one registry per `AgentExecutionService`) | Reset by `_reset_for_new_run`; safe today because 1 BOSS / subprocess |
| `ChildAgentFactory._total_created` | NOT shared | SHARED | TOCTOU on this counter — see Q-extra |
| `RetryPolicy` state | NOT shared | SHARED | Reset per run |
| `SharedContextPort` (event store backed) | **SHARED** (events table) | SHARED | OCC via UNIQUE constraint, not advisory lock |
| `expected_version` parameter on `append` | n/a | n/a | Accepted but not used in SQL — only the UNIQUE constraint enforces OCC |
| `aggregate_id` (UUIDv4) | unique by construction | unique by construction | No collision risk |

---

## Summary of severities in this scope

| Q | Topic | Severity | Real bug? |
|---|---|---|---|
| Q1 | Aggregate identity | P3 | No |
| Q2 | OCC sequence races | P2 | Misleading API; otherwise OK |
| Q3 | Replay/load races | P3 | No |
| **Q4** | **Parent-notifier OCC retry hang** | **P1** | **YES — primary finding** |
| Q5 | Aggregate ID collisions | P3 | No |
| Q6 | Connection-pool sizing | P2 | No correctness issue |
| Q7 | Implicit locks / advisory locks | P3 | None present |
| Q8 | Cross-run cache leakage | P3 | Latent; safe today |
| Q9 | Wall-clock ties in projections | P2 | Display flicker only |
| **Q-extra** | **ChildAgentFactory TOCTOU** | **P1** | **YES — when `max_total_agents` set** |
