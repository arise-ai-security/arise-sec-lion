# Concurrency Invariants

This document is the runtime contract the parallel matrix runner depends on. Every audit finding addressed by the concurrency-correctness design spec (`docs/superpowers/specs/2026-05-11-concurrency-correctness-design.md`) is an instance of one of the three invariants below. The invariants themselves are the architecture; the helper modules and code sites listed at the bottom of this doc are how they are enforced.

## Invariant F — Files

Every file write that participates in run output is (a) scoped to a per-run directory
`runs/<root_id>/...` and (b) performed via
`infrastructure/io/atomic_write.{write_text,write_bytes,write_json}` which does
`mkstemp(dir=target.parent) → write → fsync → os.replace`. No bare
`path.write_text()` or `open(path, "w")` against final paths. Per-call temp files use
`tempfile.mkstemp` / `tempfile.TemporaryDirectory` only.

## Invariant D — Database (event store)

All aggregate state changes go through `AgentSession` domain methods → `DomainEvent`
→ `EventStorePort.append_batch`. OCC enforced by
`UNIQUE(aggregate_id, sequence_number)`. No in-memory counters drive correctness
decisions (limits, totals, ordering); any such check is either derived from the
event log under OCC or guarded by an `asyncio.Lock`-protected
`reserve()` / `commit()` pair. Upward propagation (parent-notifier) must be resilient
to OCC retry loss — a child's terminal event must reach the root even if a sibling's
notification path already drove the parent to COMPLETED / FAILED.

## Invariant E — External IO + Worker locality

(a) All **retryable** external calls (currently: LLM API calls) go through
`infrastructure/io/robust_call.run` with timeout, retry policy (jitter where
idempotent), and structured error. Non-retryable external calls (Docker CLI,
ad-hoc subprocesses) still require an explicit timeout boundary — typically
`asyncio.wait_for` at the lowest async-subprocess call site — but do not use
`robust_call.run` because retry semantics do not apply (see audit-4 #6 in
"Out of scope").

(b) Every worker tool call MUST execute inside the worker's container, never the
host. Shell tools route through `docker exec`; file tools either operate on
bind-mounted paths or route through `docker exec`. No worker tool may invoke
`subprocess.Popen` against the host directly. `ClaudeCodeWorker`
(`infrastructure/workers/claude_code_worker.py:_wrap_with_docker_exec`) is the
reference implementation; `OpenHandsAdapter` must be aligned.

(c) Worker / container resources are labeled (`arise.root_id`, `arise.agent_id`,
`arise.session_pid`, `arise.created_at`) and cleaned up by label, not by name.

## Concurrency model

- 10 OS subprocesses, no Python IPC.
- Inter-process coordination uses existing durable shared state: Postgres OCC, Docker
  daemon labels, atomic file ops, and randomness (jitter desynchronizes simultaneous
  retries).
- No coordinator process, no `multiprocessing.Manager`, no message queue.

## Where these invariants are enforced

| Invariant | Helper module / code site |
|---|---|
| F | `infrastructure/io/atomic_write.py` (Phase 1) |
| D | `core/domain/aggregates/agent_session.py` (idempotence guard, Phase 2 D.1); `core/application/services/lifecycle/child_factory.py` (reservation, Phase 2 D.2); `infrastructure/adapters/postgres_event_store.py` (UNIQUE-constraint OCC) |
| E (a) | `infrastructure/io/robust_call.py` (Phase 1) — retryable external calls; Docker calls use direct `asyncio.wait_for` per Phase 3 E.11 |
| E (b) | `plugins/security/mcp/security_tools_server.py` `shell_in_container` (Phase 3 E.10); `infrastructure/workers/claude_code_worker.py:_wrap_with_docker_exec` |
| E (c) | `plugins/security/docker_runtime.py` (labels added in Phase 3 E.3); `infrastructure/cleanup/registry.py` (Phase 1) registers the label-based cleanup handler |


## Open concurrency gaps (still-true residuals)

Salvaged from the retired pre-remediation `concurrency-audit-1..5` snapshot; these remain true in current code:

- **`.last_run.json` written unconditionally** at `presentation/cli.py:208` (not gated on `ARISE_RUN_RESULT_PATH`) — a shared interactive-UI pointer that races under parallel runs. Mitigated for the matrix runner, which passes a per-invocation `ARISE_RUN_RESULT_PATH`.
- **No worker-container resource caps**: `docker_runtime.py` issues `docker run` with no `--cpus`/`--memory`, and `SecurityConfig` exposes no `worker_cpus`/`worker_memory`; high `--parallel` can saturate the host.
- **`--network host`**: worker containers run with `network_mode='host'` (`docker_runtime.py`), not an isolated network.
