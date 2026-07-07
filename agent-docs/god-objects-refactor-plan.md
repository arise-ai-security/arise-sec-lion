# God-objects refactor plan (Phase 0 output, 2026-07-07)

> **What this is.** The ranked worklist for the behavior-preserving god-object decomposition
> sweep. Produced by 6 read-only area audits (core/application, core/domain+query,
> infrastructure, plugins/security, shell layers, experiments/shared) + AST scan + fan-in map
> at commit `c1e2b0f`. Progress ledger: `agent-docs/god-objects-refactor-progress.md`.
> Recipes: A = `split-module` (package + re-export shims), B = `extract-collaborators`
> (constructor-injected collaborators, original becomes thin coordinator).

## Baseline invariants (must hold after every target)

- `uv run pytest -q` → 1215 passed / 3 pre-existing failures / 13 skipped (see ledger).
- `uv run pyright` → 0 errors. `scripts/check_architecture_boundaries.py` → clean.
- Hexagonal import matrix unchanged; `bootstrap/composition.py` stays the ONLY plugins importer.
- The 3 `AgentOrchestrator` methods stay direct methods (no pipeline/strategy wrapper).
- `plugins/` content stays strictly cybersecurity; nothing moves in or out of `plugins/`.

## Ranked targets (worst-first)

| # | Object | Location | LOC / fan-in | Conflates | Smell | Recipe | Risk | Layer / category |
|---|---|---|---|---|---|---|---|---|
| 1 | `AgentOrchestrator` | `core/application/agent_orchestrator.py` | 1677 / 19 | LLM ops + malformed-output recovery heuristics + decomposition-contract enforcement/catalog-DAG + procedure dispatch + recon/boss-block propagation | CONFLATION + LINCHPIN | B | HIGH | core/application · topological |
| 2 | `AgentExecutionService` | `core/application/execution_service.py` | 1310 / 14 | system loop/timeout/reap + flat-mode path + post-step retry ladder + workspace setup (re-inlined `WorkspaceContextProvider`) + step dispatch | CONFLATION + LINCHPIN | B | HIGH | core/application · topological |
| 3 | `OpenHandsAdapter` | `infrastructure/adapters/worker/openhands_adapter.py` | 1622 / 4 | container tool registration/path mapping + session exec + event conversion + cost/usage extraction + shared-code capture + OS process reaping | CONFLATION (+module AGGREGATION) | B (staged) | HIGH overall; pure slices LOW | infrastructure · topological |
| 4 | `Settings` models | `config/settings.py` | 734 / 13+23 | 9 disjoint config domains + YAML loader in one file | AGGREGATION | A | MED | config · topological |
| 5 | `AgentQueryService` | `core/application/services/query/query_service.py` | 686 / — | CQRS reads + DAG-readiness scheduling (10 helpers) + `SiblingViewPort` impl | CONFLATION | B | MED | core/application · topological |
| 6 | `DockerSecBenchRuntime` | `plugins/security/docker_runtime.py` | 708 / 2 | image acquisition + workspace mirroring + anti-leak sealing/script templating + session lifecycle + subprocess plumbing | CONFLATION | B | MED | **plugins/security · cybersecurity — all extractions stay inside `plugins/security/`** |
| 7 | `ClaudeCodeWorker` | `infrastructure/workers/claude_code_worker.py` | 733 / 2 | CLI invocation assembly + subprocess watchdog/kill + JSONL-transcript→event parsing | CONFLATION | B | MED (parser slice LOW) | infrastructure · topological |
| 8 | events routes | `query/api/routes/events.py` | 482 / — | HTTP routes + stateful `IncrementalHierarchyTracker` (used only here) + event→DTO mapper | CONFLATION | B | MED (SSE timing-sensitive) | query/api · topological |
| 9 | `Summary(/Incremental)Projection` | `core/query/projections/impl/summary.py` | 461 / — | 5 fused accumulators (cost/timing/errors/roles/counts) duplicated across batch AND incremental paths | CONFLATION + duplication | B | MED | core/query · topological |
| 10 | LiteLLM module | `infrastructure/adapters/litellm_adapter.py` | 790 / 2 | cohesive transport class + 2 unrelated free-function families (content tool-call parsing ~200 LOC; Anthropic cache application ~100 LOC) | AGGREGATION (module-level) | A | LOW | infrastructure · topological |
| 11 | `AgentSession` (slice) | `core/domain/aggregates/agent_session.py` | 1153 / 39 | ONLY genuine conflation: ~45 LOC child-result string rendering (`:223-252`) inside the aggregate | LINCHPIN (slice extraction) | B (slice) | LOW-MED slice; deep sub-state split DEFERRED | core/domain · topological |
| 12 | `composition.py` (slice) | `bootstrap/composition.py` | 433 / low | ONLY safe slice: `_docker_pid_cleanup` (`:255-299`, plugins-free) → `infrastructure/cleanup/`; the rest MUST stay (sole-plugins-importer invariant) | LINCHPIN (slice) | B (slice) | LOW slice | bootstrap→infrastructure · topological |
| 13 | `get_application` | `bootstrap/application.py` | 274 / low | linear 14-collaborator wiring → private sub-factories behind unchanged facade | LINCHPIN (optional) | B | MED, readability-only — OPTIONAL | bootstrap · topological |

### Deferred — experiments/shared (in-flight b4/n1 studies; scorer + live entry points)

| Object | LOC | Smell | Recipe | Defer reason |
|---|---|---|---|---|
| `evaluation/criteria.py` | 1383 | AGGREGATION of 3 wired clusters (BEF metrics / contract verdict / judge-prompt builders) — cut by CONCERN; **no obsolete subset exists** (SYSTEM_REFERENCE §IV banner claim is stale) | A | scorer consistency across replicates mid-study |
| `harness.py` | 696 | CONFLATION: study loading + subprocess lifecycle + docker cleanup + run persistence | B | live entry point of in-flight runs |
| `scripts/run_matrix.py` | 581 | CONFLATION (mild): dispatch + janitorial sweep (duplicates harness docker-cleanup → ONE shared collaborator when done) | B | active batch runner; sweep reaps by liveness |
| `evaluation/common.py` | 448 | mild AGGREGATION (tool-categorization cluster separable) | A | low value; opportunistic later |

## Phased order (risk-adjusted ROI; one target = one commit/PR; stop for review between phases)

- **Phase 1 — aggregation splits (cheap, safe):** #4 settings.py (zero-churn: `config/__init__` already re-exports all 26 names), #10 litellm module split. Plus a docs-only drift-fix commit (list below).
- **Phase 2 — easy conflations (pure seams, strong tests):** #9 summary accumulators (kills the batch/incremental duplication), #8 events-routes extraction, #7 `ClaudeTranscriptParser` slice, #5 query_service 3-way split (seam pre-exists in `ExecutionServiceDependencies`).
- **Phase 3 — deep adapter/plugin decompositions:** #6 docker_runtime collaborators (ImageEnsurer / WorkspaceMirror / RuntimeSealer / DockerCli — all inside `plugins/security/`), #3 openhands_adapter STAGED: event-converter → cost-extractor → tool-registrar module; the process-reaper cluster last or deferred (OS/timing-sensitive).
- **Phase 4 — linchpins (last, behind the strongest tests):** #2 execution_service (FlatModeRunner / PostStepHandler / re-extracted WorkspaceContextProvider), #1 agent_orchestrator (AssessmentRecovery / DecompositionContractEnforcer / ReconPropagation collaborators; assess/evaluate/execute stay direct), #11 agent_session rendering slice, #12 composition cleanup slice, #13 application.py (optional).
- **Deferred:** experiments/shared table above, until b4/n1 studies land.

Convergence (two consecutive clean passes): all non-deferred targets ✅ · gate green · no new
import cycles/boundary violations · no non-leave-alone file re-flagged by a re-audit.

## Leave-alone (cohesive by design — do NOT split; verified reasons)

| File | Reason (from audit) |
|---|---|
| `core/domain/events/events.py` (625, fan-in 103) | cohesive frozen-event catalog; documented single-file convention; split payoff marginal vs churn on the repo's #1 fan-in module |
| `infrastructure/adapters/postgres_event_store.py` (858) | read-side queries ARE the `EventStoreReadPort` contract (ISP split already gives narrow deps); all methods share one reason to change (asyncpg pool + JSONB) |
| `core/domain/services/subtask_parser.py` (724) | two public parsers over the SAME format family sharing validation machinery; isinstance warnings inherent to untyped LLM JSON |
| `core/application/services/prompt/prompt_builder.py` (518) | all build_* share one template chain; fix is a frozen `PromptContext` parameter object (polish), not a split |
| `core/application/services/toolset/tool_calling_service.py` (352) | 196-LOC loop held together by the append-only cache-stability invariant |
| `plugins/security/procedures.py` (623) | single bounded context (validator tier); patch validation reads the exploit verdict — coupled by design |
| `plugins/security/prompt_strategy.py`, `plugin.py`, `mcp/security_tools_server.py` | one strategy / thin port coordinator / one deployable unit |
| `core/domain/shared_context.py` (425) | ALREADY the reference Recipe-B implementation (SharedStore delegating to ArtifactStore/DecisionLog) |
| `query/api/schemas.py` (458) | FastAPI-idiomatic layer-scoped DTO file, section-organized; reserve zero-churn package split for growth |
| `presentation/*` (cli, run_persistence, prompt_trace_formatter) | SRP orchestrator / cohesive run-metadata I/O / textbook Strategy family |
| `infrastructure/adapters/recon_tool_adapter.py`, `openrouter_adapter.py`, `worker/shared/*`, `cleanup/registry.py` | single-responsibility adapters/utilities (openrouter↔litellm cross-file DRY noted as future consolidation only) |
| `bootstrap/bootstrap.py` (434) | idiomatic argparse entry; optional `_finalize_run()` extraction is polish, not a god object |
| `core/domain/values/`, `evaluation/models.py`, `judge.py`, `bef.py`/`linear.py`, `loading.py` | small single-concern modules; high fan-in of models.py is healthy DIP shape |

## Doc drift to fix (docs-only commit, Phase 1)

1. `architecture.md` tree omissions: `infrastructure/workers/` (flat-mode `WorkerPort` layer — ClaudeCodeWorker/OpenHandsWorker, wired via `composition.py::_build_flat_worker`), `infrastructure/adapters/openrouter_adapter.py` (live `LLMPort`), `infrastructure/cleanup/`, `config/overlay.py` + `config/_paths.py`; document WorkerPort (flat) vs WorkerToolPort (hierarchical) as a pair.
2. `patterns.md` "add a new event" recipe omits the `EVENT_TYPE_REGISTRY` update in `postgres_event_store.py:54` (event silently fails DB round-trip) and doesn't mention the second aggregate's `_apply`.
3. `SYSTEM_REFERENCE.md` §IV stale: `criteria.py` no longer carries the OBSOLETE banner; `evaluate_run` + judge-prompt builders are production-wired (post `fa51676`).
4. `presentation/cli.py` docstring references nonexistent `bootstrap/cli_main.py` (actual: `bootstrap/bootstrap.py`).
5. `agent-docs/agentic-tree-topology-research.md:939` says 35 event types; actual 38.

## Out-of-scope findings (behavior-changing — separate passes, NOT this refactor)

- **Swallowed exceptions (5 actionable):** `subtask_parser.py:300`, `event_broadcaster.py:103`, `openhands_adapter.py:608, :707`, `docker_runtime.py:479`. (`judge.py:198` reviewed: documented best-effort cost telemetry, intentional.)
- **Private cross-module imports to publicize:** `procedures.py:38` imports `prompt_strategy._role_from_task`; `openhands_adapter.py:28` + `run_matrix.py:39` import `cleanup.registry._pid_alive`.
- **Dead-code suspects:** `stop_session` (docker_runtime `:340` + protocol) has no production caller (containers reaped via PID-labeled cleanup); `plugins/security/benchmark_result.py` has no non-test importer.
- **Boundary drift (design review):** `core/application/run_invariants.py:118-130` reads `settings.security.*` inside core — config-key coupling to a domain name, invisible to the pre-commit hook.
- **Missing files behind the 3 pre-existing test failures:** `experiments/shared/scripts/analysis/` module; `experiments/shared/templates/study/configs/{C1,C2}-*.yaml`.
