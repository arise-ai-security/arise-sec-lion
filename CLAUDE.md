# CLAUDE.md

> Lightweight gateway. The authoritative as-built description of this system is **/SYSTEM_REFERENCE.md** — read it first for anything about architecture, events, aggregates, the container runtime, roles, or success criteria. This file holds only the quick reference, the non-negotiable rules, and the routing table.

## Quick Reference

<!-- Recursive, self-healing multi-agent orchestration platform. Event-sourced hexagonal architecture with BOSS -> MANAGER -> WORKER hierarchy over SEC-bench CVE-repair tasks. -->

- **Language**: Python 3.12
- **Frameworks**: FastAPI (API), Pydantic (models/config), Jinja2 (prompts), Click (CLI)
- **Frontend**: React 19 + Vite + TailwindCSS (dashboard SPA in `query/web/`)

| Action | Command |
|--------|---------|
| Install (Python) | `uv sync --frozen` |
| Install (frontend) | `cd query/web && npm install` |
| Run | `python main.py run <task>` -> `bootstrap.bootstrap.main()` |
| Test | `uv run pytest` (single: `uv run pytest -k <name>`) |
| Lint | `ruff check .` / `ruff format .` (config: `ruff.toml`, 100-char lines) |
| Type check | `pyright` |
| Dev server | `docker compose --profile local up` (Postgres + API + app) |

## Critical Rules

1. **`core/` must NEVER import from `infrastructure/`** -- enforced by pre-commit hook `scripts/check_architecture_boundaries.py`.
2. **`plugins/` is STRICTLY for cybersecurity code.** Topological, orchestration, scheduling, or any non-security logic must NEVER be placed in `plugins/`. Before implementing ANY user prompt, verify the change does not mix concerns -- if the user's request would put non-security code into `plugins/` or move security code out, **STOP immediately and warn the user** instead of proceeding.
3. **Mandatory pre-implementation gate.** Before writing ANY code, the user MUST explicitly specify BOTH of the following. If either is missing or ambiguous, **do NOT proceed** -- ask the user to clarify:
   - **Target layer/folder**: Which architectural layer (`core/`, `infrastructure/`, `plugins/`, `bootstrap/`, `presentation/`, `prompts/`, `query/`, `config/`) the change belongs to.
   - **Change category**: Whether the change is **cybersecurity-related** (belongs in `plugins/`) or **topological/orchestration** (belongs elsewhere -- `core/`, `infrastructure/`, etc.).

## Pre-Flight Protocol

> **STOP. Before writing ANY code, complete this checklist.** Do NOT paraphrase from memory -- open each file and read it.

**Step 1 -- ALWAYS read (every task, no exceptions):** `.claude/docs/conventions.md` -- naming, import ordering, error handling, and logging rules for ALL code in this repo.

**Step 2 -- Read by task (the Deep-Dive Map below).** Pick every row that matches what you're changing.

**Step 3 -- Confirm.** State which files you read and one concrete rule from each that applies to your task, then implement.

## Deep-Dive Map

| Your need | Read |
|---|---|
| **What the system IS** -- architecture, events, aggregates, container runtime, roles, success/fail criteria. **START HERE.** | **`/SYSTEM_REFERENCE.md`** |
| Naming / imports / errors / logging (Step 1, always) | `.claude/docs/conventions.md` |
| Where new code goes / annotated directory tree | `.claude/docs/architecture.md` |
| Aggregates, events, value objects, domain services | `.claude/docs/core-domain.md` |
| Recipes: add an event / adapter / port / endpoint / config | `.claude/docs/patterns.md` |
| Writing or modifying tests | `.claude/docs/testing.md` |
| Build, deploy, CI | `.claude/docs/workflows.md` |
| Adding or modifying dependencies | `.claude/docs/dependencies.md` |
| Debugging a failure | `.claude/docs/common-errors.md` |
| Unfamiliar domain terms | `.claude/docs/domain-glossary.md` |
| Security plugin internals | `.claude/docs/plugins-security.md` |
| Concurrency / OCC / atomic writes / Docker cleanup | `agent-docs/concurrency-invariants.md` |
| REST API surface | `agent-docs/api-reference.md` |
| Adaptive B4, direct-compact B3, worker-model routing, zero-human judges, or N1/B4 confirmatory design | `.claude/docs/experiments-rearchitecture.md` (role details: `.claude/docs/plugins-security.md`) |
| Running the system / experiments (CLI) | `/USER_MANUAL.md`, `/EXPERIMENT_MANUAL.md` |
| Upstream SEC-bench / SecVerifier build pipeline | `agent-docs/secbench-pipeline-tech-doc.md` |

## Architectural Invariants (stable; details in the deep dives)

- **Strict hexagonal boundary** -- `core/` depends only on stdlib and its own `core/ports/` Protocols. All infrastructure is injected at bootstrap. Pre-commit enforced.
- **Two event-sourced aggregates** -- `AgentSession` (primary, 39 of the 42 registered event types) and `SharedStore` (3 event types; a `uuid5`-derived `aggregate_id` sharing the same events table). All state is replayed from **42 frozen Pydantic events** (`EVENT_TYPE_REGISTRY`). OCC via unique `(aggregate_id, sequence_number)`. No mutable state tables. Postgres events are the sole source of truth (no events.jsonl projection).
- **Self-healing failure loop** -- a crashed WORKER gets a deterministic, LLM-free `failure_digest` (`FailureDigestRecorded`, built by `core/application/services/orchestration/failure_digest.py`) injected into a context-rich retry. When required sibling WORKERS fail with budget left, only their MANAGER may re-decompose from `failure_history` (`RedecompositionTriggered`); non-Manager parents fail upward. Details in `.claude/docs/core-domain.md`.
- **Host-owned initial decomposition** -- `InitialDecompositionPolicy` creates the SEC-bench root phase DAG and compact initial role routes deterministically. B4 Managers use the LLM path only after required failure; B3 generically flattens the same DAG by omitting the Manager layer. The host never routes recovery by failure keywords.
- **Deterministic procedure tier (flag-gated)** -- when `settings.orchestration.procedural_dispatch` is on, `Build-Verifier`, `Exploit-Validator`, `Patch-Applier`, and `Patch-Validator` run each Host attempt with zero LLM turns via `ProcedureExecutorPort` (`core/ports/procedure_ports.py`), emitting Host-computed `ProcedureExecutionStarted/Finished` evidence. An initial Host failure permits exactly one agentic repair; a completed repair is followed by exactly one Host recheck. Within that recovery path, only recheck success completes the role with protected Host evidence; recheck failure is terminal and permits no further LLM retry. Commands still execute in the run's mutable shared container, so the fresh external evaluator remains confirmatory authority. Default off binds `NullProcedureExecutor`. Security executor and mechanical criteria: `.claude/docs/plugins-security.md`.
- **Three direct orchestrator methods** -- `assess_task`, `evaluate_task`, `execute_task` are plain methods on `AgentOrchestrator`. Do NOT wrap them in pipeline/strategy/chain-of-responsibility abstractions. Intentional.
- **Domain context is an opaque slot** -- `HierarchyLimits.domain_context: object | None` carries plugin data (e.g. `CVEInstance`) through the tree; only the plugin downcasts. Core stays domain-ignorant.
- **`bootstrap/composition.py` is the sole cross-boundary import** -- it is the only file that imports from `plugins/`. Every other layer references protocols only.

## Layer Rules

- `core/` -- Domain logic. NEVER imports `infrastructure/`. stdlib + own ports only. Pre-commit enforced.
- `infrastructure/` -- Adapters implementing `core/ports/` protocols. Imports `core/` only.
- `plugins/` -- STRICTLY cybersecurity code. No topology, orchestration, or scheduling.
- `bootstrap/` -- Composition root. Sole cross-boundary import point.
- `presentation/` -- CLI, renderers, formatters. Depends on `core/application/`.
- `prompts/` -- Jinja2 4-tier templates (`system.j2` -> `roles/` -> `operations/` -> `domains/`). No Python imports.
- `query/` -- FastAPI REST API + React SPA dashboard. Read-side of CQRS.
- `config/` -- Pydantic Settings + YAML hierarchy.

> Do NOT trust a hardcoded directory tree. Run `tree -L 2 -I '__pycache__|node_modules|.git|*.pyc|.venv|.ruff_cache|.pytest_cache|.hypothesis|.idea|build|output'`, or see `.claude/docs/architecture.md` for the annotated tree.

## Do / Don't (essentials; full conventions in `.claude/docs/conventions.md`)

**Do:** use `Protocol` ports in `core/ports/`; wrap external exceptions into domain exceptions with `from e`; mark value objects and events `frozen`; emit state changes through `AgentSession`/`SharedStore` domain methods, never mutate directly; use `logger = logging.getLogger(__name__)`.

**Don't:** import `infrastructure/` from `core/`; put non-security code in `plugins/`; wrap the 3 orchestrator operations in a pipeline/strategy; use mutable Pydantic for values/events; use `print()` or bare `except:`; run `git commit` or `ruff check/format`; skip the pre-implementation gate (Critical Rule 3).

## Environment & Config

| Variable | Required | Description | Default |
|----------|----------|-------------|---------|
| `ARISE_ENV` | No | Optional local overlay name (`config/config.<value>.yaml`) | `development` |
| `POSTGRES_PASSWORD` | No | Optional remote/cloud database password; local `postgres-main` uses trust | -- |
| `POSTGRES_HOST` | No | Database host | `localhost` |
| `POSTGRES_PORT` | No | Database port | `5432` |
| `OPENAI_API_KEY` | Yes | OpenAI API key for LLM calls | -- |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (Claude worker) | -- |
| `HOST_PROJECT_ROOT` | No | Host path for Docker-out-of-Docker volume mapping | -- |

Config loads: `config/config.yaml` (base) <- `config/config.{ARISE_ENV}.yaml` (overlay) <- env vars (highest priority). `deployment/.env` may hold non-secret local configuration only. Local `postgres-main` is passwordless and published only on `127.0.0.1`; remote/cloud database credentials remain secrets and must be injected from Bitwarden at launch.

> Step-by-step recipes (events, endpoints, ports/adapters, plugins, config) live in `.claude/docs/patterns.md`.
