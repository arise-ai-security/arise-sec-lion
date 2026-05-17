# Concurrency Audit (Agent 3): Cross-Run Shared State

Scope: **host-level shared state across N parallel `main.py run` subprocesses dispatched by `experiments/shared/scripts/run_matrix.py` under `--parallel >1`.** Out-of-scope (covered by other agents): event store internals, LLM/worker adapter internal state, docker daemon mechanics.

Method: read-only inspection of the matrix runner, harness, bootstrap, config loader, run persistence, snapshot, write_report sidecar, batch scripts, docker-compose, and adapters that read environment or hit the filesystem during a run.

**Framing.** This is not a virgin audit. The codebase has already been hardened against parallel-dispatch races in commits referenced as `N-1`, `N-2`, `N-4`, `N-9`, `N-11` (see `experiments/shared/scripts/project_events.py:84-141`, `register_run.py:111-134`, `harness.py:6-12`, `write_report.py:215-273`). Below I report what remained after those fixes, plus places where the existing comments under-state the residual hazard.

---

## Run lifecycle / matrix runner

### Q1 — Run ID generation

**Finding.** Each `main.py run` subprocess generates `root_id = uuid4()` once, inside `core/application/execution_service.py:230` (`ExecutionService.create_boss_agent`). UUIDv4 collision probability is negligible (122 random bits). The harness does NOT pre-generate the run ID; it discovers it via the per-invocation result file `ARISE_RUN_RESULT_PATH` (`experiments/shared/harness.py:48,291-295`), which the CLI atomically populates from inside the child (`presentation/persistence/run_persistence.py:202-217`). No deterministic-from-args derivation anywhere in the run path.

**Severity.** Safe.

**Evidence.**
- `core/application/execution_service.py:230` — `root_id = uuid4()`
- `experiments/shared/harness.py:200-223` — `_read_run_result` validates `boss_id` and the file is unique per invocation.

**Fix.** None needed.

---

### Q2 — Output directory contention

**Finding.** Per-run output dir is `<output_directory>/<root_id>` (UUID4 stem). Created via `Path.mkdir(parents=True, exist_ok=True)` in `core/application/execution_service.py:753-754` (and again in `infrastructure/snapshot.py:44`, `presentation/persistence/run_persistence.py:188,237`). Every file that lands inside is either:
- placed in `<root_id>/...` (so the path includes the UUID), or
- a per-invocation `tempfile.mkstemp(...)` (e.g. the harness's `.run-result-...json` at `experiments/shared/harness.py:291`, the snapshot's `effective_config.yaml.<rand>.tmp` at `infrastructure/snapshot.py:52`, the manifest tmp at `presentation/persistence/run_persistence.py:134`, the per-worker `claude-cfg-*` at `infrastructure/workers/claude_code_worker.py:156,228`).

Each of these is then atomically renamed to its target — so even cross-run replays of the same final filename (e.g. `run_manifest.json`) never collide because the staging path is unique and the target lives inside the per-run UUID directory.

**Severity.** Safe.

**Evidence.**
- `core/application/execution_service.py:750-754` — `base_output / str(root_id)` with `mkdir(..., exist_ok=True)`.
- `presentation/persistence/run_persistence.py:131-145` — `_atomic_write_json` (tmp + rename in same dir).
- `experiments/shared/scripts/register_run.py:120-131` — `mkstemp`-based atomic rewrite of `run_manifest.json` (explicit audit-N-11 fix).

**Fix.** None needed.

---

### Q3 — Shared cross-run files in `runs/`

**Finding (HIGH).** `runs/.last_run.json` is a single-slot pointer rewritten unconditionally by every CLI run via `presentation/cli.py:212`:

```python
self._persistence.save_last_run(root_id, task_description, status)
self._persistence.maybe_write_run_result(root_id, status)
```

The harness explicitly added `ARISE_RUN_RESULT_PATH` (`experiments/shared/harness.py:48`) so its OWN bookkeeping no longer relies on `.last_run.json`. But the CLI still writes `.last_run.json` on every run — see `presentation/persistence/run_persistence.py:175-195` (`save_last_run` → `_atomic_write_json`). The write itself is atomic (tmp + rename), so the file is never torn. However:

- Under `--parallel 20`, `.last_run.json` ends up containing whichever run happened to finish last.
- Any interactive command that runs concurrently with a matrix (`python main.py events`, `python main.py summary`, `python main.py prompts` — they all default `agent_id` from `RunPersistence(...).get_last_run_id()`, see `bootstrap/bootstrap.py:389-402`) will read a `boss_id` that points to a near-random sibling run.

This is **wrong attribution**, not just a UX nit — interactive consumers cannot tell which run they read. Per rubric this is P0 (wrong attribution) **with mitigation**: the matrix's machine-readable path uses `ARISE_RUN_RESULT_PATH` and is unaffected.

I also observed concrete on-disk evidence of an orphaned harness result file under the runs pool:

```
/Users/garfield/PycharmProjects/arise-sec-lion/runs/.run-result-A1-openjpeg.cve-2016-7445-7feyqqys.json
```

It is 0 bytes (May 11 13:20). `experiments/shared/harness.py:313-314` unlinks the result path in a `finally`, so this orphan only appears when the harness's parent process was killed (SIGKILL) before reaching `finally`. P2 (cleanup, not correctness).

Beyond `.last_run.json`, **no other shared registry/index file** in `runs/` is written cross-run. `experiments/<study>/reports/enrollment.lock.yaml` is regenerated by `collect_study` (`experiments/shared/scripts/collect.py:244-261`), but that runs once after the matrix completes (`run_matrix.py:122`), not per dispatch. No `runs/_legacy/` writes remain (the recent commit `df6b207` deleted that walker entirely).

**Severity.** P0 (wrong attribution for interactive queries) + P2 (orphaned `.run-result-*` files survive SIGKILL).

**Evidence.**
- `presentation/cli.py:212` — unconditional `save_last_run` after every run.
- `presentation/persistence/run_persistence.py:156-195` — `FILENAME = ".last_run.json"`, single-slot.
- `bootstrap/bootstrap.py:389-402` — `_resolve_agent_id` falls back to `get_last_run_id()`.
- `experiments/shared/harness.py:313-314` — `finally: result_path.unlink(missing_ok=True)` is best-effort.

**Recommended fix.**
1. Gate `save_last_run(...)` on `ARISE_RUN_RESULT_PATH` being **unset** (the same env var the harness uses to mark a non-interactive parallel dispatch). When the harness has the result-file path, the CLI should skip the shared pointer. One-line guard at `presentation/cli.py:212` or inside `save_last_run` itself.
2. (Optional, P2) Sweep stale `.run-result-*` files older than e.g. 24h on `run_matrix` startup; add the prefix to `.gitignore` if not already.

---

### Q4 — `logs/` writes

**Finding.** No file-based logging is configured anywhere in the run path. Every `logging.basicConfig` call (`experiments/shared/scripts/run_matrix.py:68`, `experiments/shared/harness.py:394`, `scripts/batch_secbench.py:31`, …) uses the default stream handler → stderr. No `FileHandler` or `RotatingFileHandler` is instantiated in `infrastructure/`, `core/`, `bootstrap/`, `plugins/`, or any matrix script.

Each subprocess writes its own stderr to the parent's pipe (`harness.py:255-260` uses `subprocess.run(..., cwd=..., env=...)` without `stdout`/`stderr` redirection, so stderr is inherited). With 20 parallel children writing to one terminal stderr you get **interleaved lines**, but per-line atomicity is guaranteed by the kernel for writes < `PIPE_BUF` (4096 bytes on macOS/Linux); the Python `logging.StreamHandler` flushes per record, so a long line longer than `PIPE_BUF` can in theory interleave. In the diagnosis doc, `matrix.log` (100k lines) shows readable output, so this is empirically fine.

**Severity.** P2 (potential interleaved long lines, not data loss).

**Evidence.**
- `experiments/shared/scripts/run_matrix.py:68` — `logging.basicConfig(level=logging.INFO, format="%(message)s")`.
- `experiments/shared/harness.py:255-260` — `subprocess.run(...)` without redirection.

**Fix.** None required for v1. If interleaving ever becomes a problem, redirect each subprocess to `runs/<root_id>/stdout_stderr.log` (the user manual already references `stdout_stderr.log` at `agent-docs/experiment-user-manual.md:156`, but only A1/A2 cells produce it today).

---

### Q5 — CSV / report writers

**Finding (CONDITIONAL P0).** `scripts/batch_secbench.py:117-136` writes `~/secbench-all.csv` (Path.home, NOT per-run) with this strategy:

```python
def upsert_csv(result: RunResult) -> None:
    rows: list[dict[str, str]] = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
    # ... in-memory mutate ...
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
```

No lock, no `tempfile` + rename, no append-only. Hazard scope:

- **Within ONE `batch_secbench.py` invocation:** SAFE. `upsert_csv` is called from `_run_with_retry` (line 706) as a sync function with **no `await` between the read and the write**. Under asyncio the event loop cannot preempt the read-modify-write window, so even with `batch-size=15` only one task runs `upsert_csv` at a time. (CPython GIL would not be enough — the asyncio non-preemption is what saves it.)
- **Across TWO `batch_secbench.py` invocations** (operator launches a second batch while the first is still running, or two terminals overlap): torn writes, lost rows. P0 (data corruption).

This is **out of the `run_matrix.py` path** — `batch_secbench.py` is a separate, older entry point. The matrix runner does not write a shared CSV; `run_matrix._write_matrix_summary` (line 369-438) writes `experiments/<study>/reports/matrix-summary.md` exactly once, after dispatch, single-process, atomically via `write_md` (`write_report.py:116-177`, also atomic).

`scripts/batch_new_fixtures.py` mirrors the same pattern with a different default CSV (`~/new_fixtures.csv`).

**Severity.** P0 conditional on cross-invocation overlap; mention but not the headline.

**Evidence.**
- `scripts/batch_secbench.py:44` — `CSV_PATH = Path.home() / "secbench-all.csv"` (shared user-home path).
- `scripts/batch_secbench.py:117-136` — read-then-rewrite with no lock.
- `scripts/batch_secbench.py:706` — synchronous call from `_run_with_retry`.
- `scripts/batch_new_fixtures.py:40` — same pattern with `~/new_fixtures.csv`.

**Recommended fix.** If operators ever run two batch_secbench invocations concurrently:
1. Wrap the read-modify-write in an `fcntl.flock` (LOCK_EX) on a sibling `.lock` file. Pattern already used in `experiments/shared/scripts/write_report.py:240-273` for the sidecar.
2. Write through `tempfile.mkstemp` + `os.replace` so partial writes never land.

---

## Environment variables & config

### Q6 — `.env` load races

**Finding.** No Python code calls `dotenv.load_dotenv()` anywhere in the repo (`grep -rEn "load_dotenv|dotenv" --include="*.py"` returns nothing under the run path). `.env` is consumed exclusively by docker-compose (`deployment/docker-compose.yml:24,52,74,159 env_file: .env`) for the container variants, and by the operator's shell (`set -a && source deployment/.env && set +a`, see `agent-docs/experiment-user-manual.md:35`) for host-launched parallel runs.

Nothing in the codebase WRITES to `.env`. No file-race possible.

**Severity.** Safe.

**Evidence.** Empty grep result for `dotenv` / `load_dotenv` outside `.venv/`.

**Fix.** None needed.

---

### Q7 — `os.environ` mutation at runtime

**Finding.** A grep for `os.environ[...] = ...` / `os.environ.setdefault` / `os.putenv` across all non-test Python returns **zero hits in the run path**. The only `os.environ` mutation is the harness building a CHILD subprocess env at `experiments/shared/harness.py:255`:

```python
env = {**os.environ, RUN_RESULT_ENV_VAR: str(result_path)}
```

This is a fresh dict for the child process, not a mutation of the parent's `os.environ`. Every other `os.environ` reference is a READ (e.g. `os.environ.get(...)` at `infrastructure/workers/claude_code_worker.py:381,398` for env-allowlist forwarding; `plugins/security/docker_runtime.py:29` for `HOST_PROJECT_ROOT`; `plugins/security/mcp/security_tools_server.py:91-93` for `ENV_*` lookups).

Different managers in the same run cannot stomp on each other's process env because no code path writes `os.environ` after startup.

**Severity.** Safe.

**Evidence.**
- `experiments/shared/harness.py:255` — child env constructed as a new dict.
- `presentation/persistence/run_persistence.py:211` — read-only.
- `infrastructure/workers/claude_code_worker.py:381,398` — read-only allowlist forwarding.

**Fix.** None needed.

---

### Q8 — Config overlay determinism

**Finding.** `config/settings.py:_load_yaml_hierarchy` (line 34-51) opens `config.yaml` and `config.<env>.yaml` afresh on every call, returning a new dict each time. `_merge_dicts` (line 23-31) is pure (recursive `.copy()` + recursion). `Settings.load` (line 509-513) and `Settings.from_yaml` (line 515-530) are class-method entrypoints; neither caches a previously-built Settings. `config/overlay.resolve_overlay` (line 26-81) reads the file at each call, builds a `_visited` frozenset locally, and returns a `deepcopy`-merged dict. No module-level mutable cache and no `lru_cache` on these functions.

The only `lru_cache` in the config/cost path is `infrastructure/adapters/cost_calculator.py:110 _get_cached_model_cost_map` (max=1), which is per-process and idempotent (LiteLLM's static price dict) — does not vary by run.

Every subprocess therefore loads its own deterministic merged config. Two subprocesses pointed at the same `config.yaml` + same `ARISE_ENV` will get bytewise-identical Settings. No partial-overlay window — the load is a single `yaml.safe_load(path.read_text())` (no streaming), so a concurrent overlay edit either finishes before the read or comes in after, never mid-read.

**Severity.** Safe.

**Evidence.**
- `config/settings.py:34-51` — fresh file reads per call.
- `config/overlay.py:26-81` — `_visited: frozenset` is per-call.
- `infrastructure/adapters/cost_calculator.py:110-125` — `lru_cache(maxsize=1)` over LiteLLM's static map.

**Fix.** None needed.

---

### Q9 — `ARISE_ENV` propagation

**Finding.** `config.settings.get_environment` (line 18-20) reads `os.getenv("ARISE_ENV", "development")` once per `Settings.load` call. The user manual instructs operators to `export ARISE_ENV=production` in the parent shell (`agent-docs/experiment-user-manual.md:37`). `experiments/shared/harness.py:255` constructs the child subprocess env by spread `**os.environ`, so every child inherits whatever the parent shell set. All N subprocesses see the same `ARISE_ENV`.

If a single matrix run tries to mix cells expecting different overlays, that is unsupported — but the cell config files use `extends: config/config.yaml` (base) not `extends: config/config.development.yaml` (see `experiments/2026-05-11-fresh-start/configs/B1-ours-claude-noverifier.yaml:5`), so cell-level isolation is achieved through the overlay file, not the env. The `ARISE_ENV` overlay only affects `Settings.load()` paths that don't use `-c <config>` (e.g. `python main.py prompts`, `python main.py list`).

**Caveat already documented** at `agent-docs/experiment-user-manual.md:163`: `ARISE_ENV=development` triggers a stale-key `extra_forbidden` validation crash in `Settings`. Operators must export `ARISE_ENV=production`. Not concurrency-related.

**Severity.** Safe (within a single matrix invocation).

**Evidence.**
- `config/settings.py:18-20` — `get_environment()` reads at call time.
- `experiments/shared/harness.py:255` — child env inherits via dict spread.
- `experiments/2026-05-11-fresh-start/configs/B1-ours-claude-noverifier.yaml:5` — cells extend `config/config.yaml` (base, env-agnostic).

**Fix.** None needed.

---

## Host-resource contention (filesystem-level)

### Q10 — Port allocation

**Finding.** No host port is bound during a `main.py run` invocation. The MCP server runs as a **stdio** subprocess (`plugins/security/mcp/security_tools_server.py:271 mcp.run("stdio")`), so it inherits pipes from the parent and binds nothing. FastAPI is only started inside the long-lived `api` compose service (`deployment/docker-compose.yml:11-34`), not during a run. SEC-bench worker containers are spawned with no published ports (the diagnosis doc explicitly confirms this at `agent-docs/parallel-20-host-saturation-diagnosis.md:231 H5 REFUTED`). No `socket.bind`, no `uvicorn.run`, no `app.run`, no `asyncio.start_server` in the run path.

**Severity.** Safe.

**Evidence.**
- `plugins/security/mcp/security_tools_server.py:271` — stdio transport.
- `deployment/docker-compose.yml:28,76` — only `api` (8000) and `db` (5432) publish host ports, and only when their compose profile is up.
- Grep for `uvicorn.run|app.run|asyncio.start_server|socket.bind` in the run path returns nothing.

**Fix.** None needed.

---

### Q11 — Temp-file naming

**Finding.** Every `tempfile.mkstemp` and `tempfile.TemporaryDirectory` call uses either a Python random suffix (`mkstemp` always; `TemporaryDirectory` always with `prefix="claude-cfg-"`) or scopes itself to a per-run directory:
- `experiments/shared/harness.py:291` — `prefix=f".run-result-{cell}-{task}-", dir=str(pool)`. Random suffix per call, in `runs/`.
- `presentation/persistence/run_persistence.py:134` — `prefix=path.name + ".", dir=path.parent`. Random suffix per call, in target dir.
- `infrastructure/snapshot.py:52` — same pattern, in the per-run dir.
- `experiments/shared/scripts/register_run.py:120`, `project_events.py:99`, `write_report.py:87` — same.
- `infrastructure/workers/claude_code_worker.py:156` — `tempfile.TemporaryDirectory(prefix="claude-cfg-")` (in `$TMPDIR`, random suffix).
- `infrastructure/workers/claude_code_worker.py:228` — `tempfile.TemporaryDirectory(dir=str(run_dir), prefix="claude-cfg-")` (in per-run dir).

`write_report.py:251` uses **a deterministic path** but for a lock file:

```python
return Path(tempfile.gettempdir()) / f"arise-sec-lion.generated-json.{digest}.lock"
```

The digest is the sha256 of the sidecar's resolved path, so two writers targeting the **same** `.generated.json` agree on the lock (intentional) and writers targeting **different** sidecars never collide. This is the only deterministic /tmp path and it is correctly used.

No `tempfile.mktemp` (the insecure form) anywhere. No `/tmp/<fixed-name>` files used for shared state.

**Severity.** Safe.

**Evidence.** Quoted above.

**Fix.** None needed.

---

### Q12 — HuggingFace / model caches

**Finding.** During a parallel matrix run the only Python entrypoint that hits HuggingFace is `scripts/batch_secbench.py:148-154` (`load_dataset("SEC-bench/SEC-bench", split="eval")`), which is **out of the `run_matrix.py` dispatch path**. `run_matrix.py` reads its dataset from `experiments/<study>/dataset.yaml` (local file, `experiments/shared/scripts/run_matrix.py:74,457-465`); there is no HuggingFace fetch during a matrix run.

No `litellm.set_verbose`, no on-disk LiteLLM cache. `infrastructure/adapters/cost_calculator.py:110-125` caches the LiteLLM price map in-process only.

`infrastructure/workers/claude_code_worker.py:156,228` creates per-run `CLAUDE_CONFIG_DIR` scratch dirs — these are short-lived, distinct, and never shared with another run.

**Severity.** Safe (within the matrix path).

**Evidence.**
- `experiments/shared/scripts/run_matrix.py:457-465` — local-YAML dataset load.
- `scripts/batch_secbench.py:148-154` — HuggingFace only here, separate entrypoint.

**Fix.** None needed.

---

### Q13 — `uv` / `pip` caches

**Finding.** `uv sync` is not called at run time. The harness invokes `python main.py run ...` (`experiments/shared/harness.py:243-254`), which assumes the venv is already populated. The operator's pre-flight (`uv sync --frozen`) is once per repo, before the matrix is launched. Not a per-run concern.

**Severity.** N/A.

**Evidence.** No `uv sync` or `pip install` call inside the run path; `main.py` is invoked directly.

**Fix.** None needed.

---

## Postgres connection-string discovery

### Q14 — Connection-string discovery

**Finding.** Each subprocess builds its DSN inside `config/settings.py:_build_from_config` (line 484-507):

```python
postgres_password = os.getenv("POSTGRES_PASSWORD")
if not postgres_password:
    raise ValueError("POSTGRES_PASSWORD environment variable is required")
db_config["password"] = postgres_password
if os.getenv("POSTGRES_HOST"):
    db_config["host"] = os.getenv("POSTGRES_HOST")
postgres_port = os.getenv("POSTGRES_PORT")
if postgres_port:
    db_config["port"] = int(postgres_port)
```

Reads env at startup. Each subprocess opens its own `PostgresEventStore` pool inside `bootstrap/bootstrap.py:137-142 _event_store(settings)`. The `query/api/app.py:51-81 lifespan` opens a separate pool for the FastAPI process (not relevant to run subprocesses).

One non-pooled risk: `bootstrap/bootstrap.py:303-323 _query_projection`, `:326-358 _list_runs`, and `:361-386 _trace_prompts` each open a fresh `_event_store(settings)` context for one-shot CLI queries. They are NOT in the parallel-run hot path (they are operator-driven). Each one opens an asyncpg pool with default sizing — but they always disconnect at context exit. No leak under load from the run path itself.

**Severity.** Safe (each run opens an isolated pool; no shared connection state).

**Evidence.**
- `config/settings.py:490-498` — DSN built from per-process env.
- `bootstrap/bootstrap.py:134-142` — `asynccontextmanager` ensures disconnect.
- `agent-docs/parallel-20-host-saturation-diagnosis.md:229` — H3 (pool exhaustion) refuted: `max_connections=300`, ~50 peak observed.

**Fix.** None needed in this scope. (Pool-sizing details delegated to agent 1.)

---

## Shared-vs-not-shared summary

| Resource | Shared? | Mechanism | Verdict |
|---|---|---|---|
| `runs/<root_id>/` | **No** | UUIDv4 directory per run | Safe |
| `runs/.last_run.json` | **Yes** | Atomic-rewrite, single slot | P0 — wrong attribution for interactive consumers |
| `runs/.run-result-<cell>-<task>-*.json` | No | `mkstemp` per harness invocation | Orphan-on-SIGKILL only (P2) |
| `runs/_legacy/`, `runs/index.json` | n/a | does not exist | — |
| `experiments/<study>/reports/*` | **Yes** | Single-process, post-dispatch | Safe (run_matrix collects after `as_completed`) |
| `experiments/<study>/reports/.generated.json` | **Yes** | `fcntl.LOCK_EX` per-sidecar digest | Safe |
| `experiments/<study>/manifest.yaml` | n/a | read-only at runtime | Safe |
| `~/secbench-all.csv` | **Yes** | Read-modify-rewrite, no lock | P0 if 2+ `batch_secbench.py` invocations overlap; safe within a single invocation |
| `~/new_fixtures.csv` | **Yes** | Same as above | Same |
| `config/config*.yaml` | Read | Fresh `yaml.safe_load` per call | Safe |
| `deployment/.env` | Read | docker-compose / shell only; no Python writer | Safe |
| `ARISE_ENV` | Read | Inherited from parent shell once | Safe |
| `POSTGRES_*` | Read | Each subprocess reads its env | Safe |
| `os.environ` (post-startup) | n/a | No mutation in run path | Safe |
| Host ports | **None bound** | MCP is stdio; FastAPI only in `api` service | Safe |
| `/tmp/arise-sec-lion.generated-json.<digest>.lock` | **Yes** (intentional) | Per-sidecar-digest lockfile under `fcntl.flock` | Safe by design |
| HF / LiteLLM / torch caches | n/a | Not used in run path | Safe |
| `uv` venv | n/a | Pre-built before matrix; not touched per run | Safe |
| `runs/<root_id>/mcp_servers.json` | No | Per-run dir | Safe |
| `runs/<root_id>/effective_config.yaml` | No | Per-run dir, atomic write | Safe |
| `runs/<root_id>/run_manifest.json` | No | Per-run dir, atomic `mkstemp` + replace | Safe |
| `runs/<root_id>/events.jsonl` | No | Per-run dir, atomic `mkstemp` + replace + fsync | Safe |
| In-harness state (`manifest`, `coverage`, `dataset`, `repo_root`) | Yes (read-only) | Built once before `ThreadPoolExecutor`, passed positionally to `_run_one`; never mutated downstream | Safe |

---

## Top hazards (rubric-honest)

1. **P0 (with mitigation) — `runs/.last_run.json` shared single-slot pointer.** `presentation/cli.py:212` unconditionally calls `save_last_run` on every CLI run. Under `--parallel 20` the file ends up pointing at whichever run finished last; any interactive `python main.py {events,summary,prompts,list}` invocation that omits `--agent-id` reads the wrong run (`bootstrap/bootstrap.py:389-402`). The matrix's own bookkeeping is unaffected because the harness uses `ARISE_RUN_RESULT_PATH` (`experiments/shared/harness.py:48,291-295`). Fix: gate `save_last_run` on `ARISE_RUN_RESULT_PATH` being unset.

2. **P0 (conditional, out of matrix scope) — `~/secbench-all.csv` lock-free read-modify-rewrite.** `scripts/batch_secbench.py:117-136`. Safe within a single `batch_secbench.py` invocation (asyncio non-preemption between read and write), unsafe across two concurrent invocations or any future refactor that introduces an `await` inside the window. Mirrors in `scripts/batch_new_fixtures.py:40`. Not on the `run_matrix.py` path — flagging for completeness. Fix: wrap with `fcntl.flock` on a sibling lock file (pattern already used in `experiments/shared/scripts/write_report.py:240-273`).

3. **P2 — Orphan `.run-result-<cell>-<task>-*.json` files survive SIGKILL.** Observed at `/Users/garfield/PycharmProjects/arise-sec-lion/runs/.run-result-A1-openjpeg.cve-2016-7445-7feyqqys.json` (0 bytes). `experiments/shared/harness.py:313-314` `finally` does not run when the parent is killed. Cleanup-only. Fix: scan and unlink stale `.run-result-*` on `run_matrix` startup, plus add the prefix to `.gitignore`.

Everything else in the matrix path is **already hardened** by audit fixes N-1, N-2, N-4, N-9, N-11 (per-run UUID dirs, `mkstemp`-based atomic rewrites, `fcntl`-locked sidecar, per-invocation env-var run-id channel, fresh-dict subprocess env). The codebase is in good shape for cross-run isolation.
