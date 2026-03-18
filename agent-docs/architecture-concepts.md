# Architecture Concepts

## Event Sourcing

State is derived by replaying immutable events, never stored directly.

```
Traditional: UPDATE agents SET status = 'completed' WHERE id = 123
This system: APPEND events (AgentCreated, TaskAssigned, WorkCompleted)
```

- `AgentSession.load_from_history(events)` replays to reconstruct state
- ~30 frozen `DomainEvent` Pydantic models in `core/domain/events/events.py`
- PostgreSQL `events` table is append-only
- Complete audit trail, time-travel debugging

## OCC (Optimistic Concurrency Control)

No locks — assume no conflict, check at write time:

```
Process A: Read agent (version=5)
Process B: Read agent (version=5)
Process B: Write event (expected_version=5) ✓ → version=6
Process A: Write event (expected_version=5) ✗ → ConcurrencyError → reload and retry
```

`AgentExecutionService.run_agent_step()` retries on `ConcurrencyError` automatically.

## CQRS

Write side (commands) and read side (queries) use different models:

- **Write**: `AgentSession` aggregate handles commands, emits events
- **Read**: `core/query/projections/` builds optimized views from events
- Projections: Summary, AgentList, Cost, AgentSummary

## Hexagonal Architecture (Ports & Adapters)

```
                  Presentation (CLI, API)
                         │
                    Application
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
          EventStore   LLM      Worker     ← Ports (core/ports/)
              │          │          │
              ▼          ▼          ▼
          Postgres    LiteLLM   Claude SDK  ← Adapters (infrastructure/)
```

**Critical rule**: `core/` never imports from `infrastructure/`. Bootstrap wires adapters to ports at startup.

## DDD (Domain-Driven Design)

- **Aggregate**: `AgentSession` — single unit for state changes, event-sourced
- **Domain Events**: Immutable facts, past tense (`AgentCreated`, `WorkCompleted`)
- **Value Objects**: Frozen Pydantic models (`NodeMessage`, `HierarchyLimits`, `Subtask`)
- **Ubiquitous Language**: BOSS, MANAGER, WORKER, PENDING, Subtask, Briefing, Report, Handoff

## SharedStore

`core/domain/shared_context.py` — event-sourced cross-agent state per execution hierarchy (keyed by `root_id`):

- `ArtifactStore` — shared outputs between agents
- `DecisionLog` — architectural/design decisions

Uses UUID5-derived aggregate_id to avoid collision with AgentSession aggregates.

## Docker-out-of-Docker (DooD)

Containers access the host's Docker daemon via mounted socket (`/var/run/docker.sock`). SEC-bench workers build/run Docker containers on the host daemon. Simpler and faster than Docker-in-Docker (no `--privileged`, shared layer cache).

**Security**: container has root-equivalent access to host's Docker. Only use in trusted environments.
