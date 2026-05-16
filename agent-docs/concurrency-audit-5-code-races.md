# Concurrency Audit 5 — Code-Level Races Inside One Python Run

Concurrency model assumed: one run is one Python subprocess with one asyncio event loop. Managers run concurrently as asyncio tasks. Workers may run in parallel subject to `max_concurrent_workers`; docker exec is serialized elsewhere and is not re-audited here.

## 1. Forgotten Await / Unmanaged Task

Finding: No forgotten `await` or unmanaged `create_task` was found in the inspected in-run orchestration path. The only in-scope `create_task` is captured in a task registry and later gathered or cancelled.

Severity: P2

Evidence:

`core/application/execution_service.py:322`
> `                tasks[agent_id] = asyncio.create_task(`

`core/application/execution_service.py:323`
> `                    self._run_agent_step_safe(agent_id)`

`core/application/execution_service.py:340`
> `            await asyncio.gather(*tasks.values(), return_exceptions=True)`

`core/application/execution_service.py:621`
> `        for task in tasks.values():`

`core/application/execution_service.py:624`
> `        await asyncio.gather(*tasks.values(), return_exceptions=True)`

Recommended minimal fix: No code change required. Keep the existing registry-and-gather pattern for future background tasks; reject bare `asyncio.create_task(...)` calls that are not stored and cancelled/awaited.

## 2. `asyncio.gather` With Mutating Closures

Finding: No `gather(*tasks)` mutating a shared list/dict closure was found in the in-run path. `ChildAgentFactory.create_children_from_events` gathers independent child-creation coroutines, and the aggregate counter update happens after the gather completes. A separate cross-evaluator counter race is covered in question 5.

Severity: P2

Evidence:

`core/application/services/lifecycle/child_factory.py:193`
> `        results = await asyncio.gather(`

`core/application/services/lifecycle/child_factory.py:194`
> `            *[self._create_child_parallel(event, parent_id) for event in events]`

`core/application/services/lifecycle/child_factory.py:197`
> `        # Update counter after all parallel operations complete`

`core/application/services/lifecycle/child_factory.py:199`
> `        self._total_created += len(events)`

Recommended minimal fix: Keep shared counter/list mutations outside gathered closures. If a future gathered coroutine must mutate shared process state, protect the mutation with an `asyncio.Lock` or return values and merge after `gather`.

## 3. `asyncio.create_task` Without Holding a Reference

Finding: No in-scope unreferenced `create_task` was found. The orchestration loop stores tasks by `agent_id`, reaps completed entries, and gathers remaining tasks on loop exit or timeout.

Severity: P2

Evidence:

`core/application/execution_service.py:300`
> `        tasks: dict[UUID, asyncio.Task] = {}`

`core/application/execution_service.py:322`
> `                tasks[agent_id] = asyncio.create_task(`

`core/application/execution_service.py:575`
> `        completed = [aid for aid, task in tasks.items() if task.done()]`

`core/application/execution_service.py:578`
> `            task = tasks.pop(aid)`

`core/application/execution_service.py:624`
> `        await asyncio.gather(*tasks.values(), return_exceptions=True)`

Recommended minimal fix: No code change required. Continue requiring every created task to be stored in an owner collection and settled in normal and cancellation paths.

## 4. `run_in_executor(None, ...)` Default Executor

Finding: The query API uses the event loop default executor for prompt-template globbing. This is outside the core run loop, but if the API serves traffic during a run it shares the process default threadpool with any other default-executor users.

Severity: P2

Evidence:

`query/api/routes/prompts.py:82`
> `async def _glob_templates(directory: Path, pattern: str) -> list[Path]:`

`query/api/routes/prompts.py:85`
> `    pathlib.glob is synchronous, so we run it in the default executor.`

`query/api/routes/prompts.py:87`
> `    loop = asyncio.get_running_loop()`

`query/api/routes/prompts.py:88`
> `    return await loop.run_in_executor(None, lambda: list(directory.glob(pattern)))`

Recommended minimal fix: Use a small dedicated executor for prompt-file operations, or replace this with async directory iteration. If prompt listing is low volume, synchronous globbing may be simpler than consuming the shared default executor.

## 5. Cross-Task Mutable Sharing in Orchestrator

Finding: Concurrent manager evaluations share `ChildAgentFactory._total_created` and `HierarchyLimitsRegistry._limits` without a lock. The total-agent limit check reads `total_created` before child creation, while child creation and counter increments happen later after agent events are persisted. Two managers running concurrently can both pass the total-agent check against the same stale count and oversubscribe `max_total_agents`.

Severity: P1

Evidence:

`core/application/execution_service.py:320`
> `            for agent_id in new_agents:`

`core/application/execution_service.py:322`
> `                tasks[agent_id] = asyncio.create_task(`

`core/application/agent_orchestrator.py:620`
> `        max_total = self._child_factory.max_total_agents`

`core/application/agent_orchestrator.py:622`
> `            current_total = self._child_factory.total_created`

`core/application/agent_orchestrator.py:624`
> `            if len(subtasks) > remaining:`

`core/application/services/lifecycle/child_factory.py:62`
> `        self._total_created: int = 0`

`core/application/services/lifecycle/child_factory.py:197`
> `        # Update counter after all parallel operations complete`

`core/application/services/lifecycle/child_factory.py:199`
> `        self._total_created += len(events)`

`core/application/services/lifecycle/hierarchy_limits_registry.py:12`
> `        self._limits: dict[UUID, HierarchyLimits] = {}`

`core/application/services/lifecycle/hierarchy_limits_registry.py:53`
> `        self._limits[child_id] = child_limits`

Recommended minimal fix: Move total-agent reservation into `ChildAgentFactory` and guard check/reserve/create with an `asyncio.Lock`. The smallest safe shape is a locked `reserve_children(parent_id, child_ids)` step before emitting children, with rollback only if persistence fails; a stronger fix is deriving the count from the event store under OCC.

## 6. Per-Run Files Written Concurrently From Multiple Tasks

Finding: No in-run Python code path was found where multiple asyncio tasks write the same `runs/<run_id>/...` file. The observed per-run writers are post-run manifest/snapshot writes or flat-mode single-worker transcript/config writes. Worker-produced artifact writes by external tools are outside this code-level audit and should remain agent 3/4 scope.

Severity: P2

Evidence:

`presentation/persistence/run_persistence.py:237`
> `        run_dir = self._output_dir / str(run_id)`

`presentation/persistence/run_persistence.py:238`
> `        manifest_path = run_dir / "run_manifest.json"`

`presentation/persistence/run_persistence.py:269`
> `        _atomic_write_json(manifest_path, payload)`

`infrastructure/snapshot.py:44`
> `    run_dir.mkdir(parents=True, exist_ok=True)`

`infrastructure/snapshot.py:57`
> `        tmp_path.replace(target)`

`infrastructure/workers/claude_code_worker.py:90`
> `        run_dir = Path(workspace.root)`

`infrastructure/workers/claude_code_worker.py:92`
> `        transcript_path = run_dir / "stdout_stderr.log"`

Recommended minimal fix: No immediate code change for Python in-run writers. If hierarchical workers begin writing per-run files directly, require agent-specific filenames/directories or a per-run file lock.

## 7. Read-Modify-Write File Loops

Finding: No in-run JSON read-modify-write file loop was found in scoped runtime code. The last-run pointer is read separately from its atomic write, and the per-run manifest writer builds a fresh payload.

Severity: P2

Evidence:

`presentation/persistence/run_persistence.py:170`
> `            data = json.loads(self._path.read_text())`

`presentation/persistence/run_persistence.py:195`
> `        _atomic_write_json(self._path, info.to_dict())`

`presentation/persistence/run_persistence.py:240`
> `        payload: dict[str, Any] = {`

`presentation/persistence/run_persistence.py:269`
> `        _atomic_write_json(manifest_path, payload)`

Recommended minimal fix: No code change required for the scoped path. For any future file-backed read-modify-write state, use a process-local `asyncio.Lock` plus atomic replace, or move the state to the event store with OCC.

## 8. `Path.mkdir(exist_ok=True)` + Immediate Write

Finding: The scoped per-run writers use `mkdir(..., exist_ok=True)` before atomic temp-file replacement. No concurrent in-run check-then-create writer to the same target file was found.

Severity: P2

Evidence:

`presentation/persistence/run_persistence.py:133`
> `    path.parent.mkdir(parents=True, exist_ok=True)`

`presentation/persistence/run_persistence.py:134`
> `    fd, tmp_name = tempfile.mkstemp(`

`presentation/persistence/run_persistence.py:142`
> `        tmp_path.replace(path)`

`infrastructure/snapshot.py:44`
> `    run_dir.mkdir(parents=True, exist_ok=True)`

`infrastructure/snapshot.py:52`
> `    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(run_dir))`

`infrastructure/snapshot.py:57`
> `        tmp_path.replace(target)`

Recommended minimal fix: Keep temp-file-in-same-directory plus `replace`. If a target can be written by multiple in-run tasks, add a per-path lock or make the target path unique per agent.

## 9. Module-Level Mutable State

Finding: Module/class-level mutable state exists, but most of it is bootstrap/import-time registry state or protected runtime subscriber state. The important runtime singleton is `EventBroadcaster`; its subscriber registry is guarded by an `asyncio.Lock`. Query API factory globals are mutable at bootstrap and should not be changed after startup.

Severity: P2

Evidence:

`core/query/projections/registry.py:17`
> `    _filters: ClassVar[dict[str, type]] = {}`

`core/query/projections/registry.py:18`
> `    _projections: ClassVar[dict[str, type]] = {}`

`core/query/projections/registry.py:19`
> `    _formatters: ClassVar[dict[str, type]] = {}`

`core/query/projections/registry.py:20`
> `    _sinks: ClassVar[dict[str, type]] = {}`

`core/query/projections/registry.py:96`
> `            registry[name] = cls_to_register`

`query/api/app.py:27`
> `_event_store_factory: Callable[[str], EventStorePort] | None = None`

`query/api/app.py:28`
> `_domain_plugin_factory: Callable[["Settings"], "DomainPlugin | None"] | None = None`

`query/api/app.py:37`
> `    global _event_store_factory`

`core/application/services/query/event_broadcaster.py:32`
> `    _instance: "EventBroadcaster | None" = None`

`core/application/services/query/event_broadcaster.py:37`
> `        self._subscribers: dict[UUID, list[asyncio.Queue[DomainEvent]]] = (`

`core/application/services/query/event_broadcaster.py:67`
> `        async with self._registry_lock:`

Recommended minimal fix: Freeze registries and API factories after application startup, or assert they are not reset while serving. Keep `EventBroadcaster` subscriber mutations under `_registry_lock`.

## 10. `functools.lru_cache` / `@cache` on Coroutine Functions

Finding: No `@lru_cache` or `@cache` decorator was found on an `async def`. All observed cached functions are synchronous schema/cost helpers.

Severity: P2

Evidence:

`core/domain/services/subtask_parser.py:52`
> `@lru_cache(maxsize=1)`

`core/domain/services/subtask_parser.py:53`
> `def build_assessment_schema_hint() -> str:`

`core/domain/services/subtask_parser.py:101`
> `@lru_cache(maxsize=1)`

`core/domain/services/subtask_parser.py:102`
> `def build_subtasks_schema_hint() -> str:`

`core/application/services/orchestration/verification_pipeline.py:37`
> `@lru_cache(maxsize=1)`

`core/application/services/orchestration/verification_pipeline.py:38`
> `def build_judge_schema_hint() -> str:`

`infrastructure/adapters/cost_calculator.py:110`
> `@lru_cache(maxsize=1)`

`infrastructure/adapters/cost_calculator.py:111`
> `def _get_cached_model_cost_map() -> dict[str, dict] | None:`

Recommended minimal fix: No code change required. Keep coroutine results out of `functools.lru_cache`; if async caching becomes necessary, cache resolved values behind an async-aware lock.

## 11. Bootstrap Singletons / Shared Instances

Finding: Bootstrap creates a single worker adapter instance and shares it across worker tasks. `WorkerAdapterBase` stores per-call timing in `self._start_time`; with `max_concurrent_workers > 1`, parallel sessions on the same adapter can overwrite each other's timing and emit incorrect worker durations/cost telemetry. This is a code-level race inside one run.

Severity: P1

Evidence:

`bootstrap/infrastructure.py:99`
> `    event_store = PostgresEventStore(config.postgres_connection_string)`

`bootstrap/infrastructure.py:100`
> `    llm_adapter = LiteLLMAdapter()`

`bootstrap/infrastructure.py:101`
> `    worker_tool = _create_worker_adapter(config)`

`bootstrap/application.py:178`
> `    orchestrator = AgentOrchestrator(`

`bootstrap/application.py:180`
> `        worker_port=infrastructure.worker_tool,`

`core/application/services/lifecycle/role_dispatch.py:97`
> `        async with self._context.worker_semaphore:`

`bootstrap/application.py:216`
> `        max_concurrent_workers=config.concurrency.max_concurrent_workers,`

`infrastructure/adapters/worker/base.py:45`
> `        self.timeout_seconds = timeout_seconds`

`infrastructure/adapters/worker/base.py:46`
> `        self._start_time: float = 0.0`

`infrastructure/adapters/worker/base.py:118`
> `        self._start_time = time()`

`infrastructure/adapters/worker/base.py:125`
> `        return time() - self._start_time if self._start_time else 0.0`

`infrastructure/adapters/worker/claude_sdk_adapter.py:95`
> `        self._start_timing()`

`infrastructure/adapters/worker/google_adk_adapter.py:148`
> `        self._start_timing()`

`infrastructure/adapters/worker/openhands_adapter.py:133`
> `        self._start_timing()`

Recommended minimal fix: Make timing state local to each `run_session` or `_execute_task` invocation. For example, compute `started_at = time()` in the local coroutine and pass elapsed seconds into cost-event construction instead of storing per-call timing on the shared adapter object.

## 12. `logging.basicConfig` Calls

Finding: No `logging.basicConfig` call was found in scoped runtime modules (`bootstrap`, `core`, `infrastructure`, `presentation`, `query`, `plugins/security`). The only matches are batch scripts that orchestrate multiple runs and are outside this one-run subprocess scope.

Severity: P2

Evidence:

`scripts/batch_secbench.py:31`
> `logging.basicConfig(`

`scripts/batch_secbench.py:36`
> `logger = logging.getLogger("batch_secbench")`

`scripts/batch_new_fixtures.py:27`
> `logging.basicConfig(`

`scripts/batch_new_fixtures.py:32`
> `logger = logging.getLogger("batch_new_fixtures")`

Recommended minimal fix: No one-run runtime change required. If these scripts are imported by other tooling, move logging setup under `if __name__ == "__main__"` or a script entrypoint.

## 13. Per-Run Log Handlers

Finding: No per-run Python logging `FileHandler`/`RotatingFileHandler` is attached in scoped runtime code. Flat-mode `ClaudeCodeWorker` captures subprocess stdout/stderr to `runs/<run_id>/stdout_stderr.log`, but this is direct file I/O, not a logging handler.

Severity: P2

Evidence:

`infrastructure/workers/claude_code_worker.py:90`
> `        run_dir = Path(workspace.root)`

`infrastructure/workers/claude_code_worker.py:92`
> `        transcript_path = run_dir / "stdout_stderr.log"`

`infrastructure/workers/claude_code_worker.py:464`
> `        with transcript_path.open("wb") as transcript:`

`infrastructure/workers/claude_code_worker.py:467`
> `                stdout=transcript,`

`infrastructure/workers/claude_code_worker.py:468`
> `                stderr=asyncio.subprocess.STDOUT,`

Recommended minimal fix: No change if process logs are intentionally global/stdout-only. If per-run Python logs are required, attach a run-scoped handler via a context manager and always remove/close it in `finally`.

## 14. QueueHandler / Multiprocessing Logging

Finding: No `QueueHandler`, `QueueListener`, `logging.handlers`, or `multiprocessing` logging setup was found in scoped runtime source. There is no queue-drain lifecycle to audit.

Severity: P2

Evidence:

No instances found by grep for `QueueHandler`, `QueueListener`, `logging.handlers`, or `multiprocessing.` in scoped runtime Python files.

Recommended minimal fix: No code change required. If queue-based logging is introduced, make the listener lifecycle owned by the run/application lifespan and stop/drain it explicitly on shutdown.

## 15. Jinja2 `Environment` Thread/Task Safety

Finding: `PromptBuilder` owns one Jinja2 `Environment` shared by concurrent prompt renders. The code treats it as immutable after construction and loads templates lazily via `get_template`. That is acceptable for one run if prompt files do not change during the run; it becomes unsafe/stale if the query API edits prompt files while this environment is active.

Severity: P2

Evidence:

`core/application/services/prompt/prompt_builder.py:95`
> `        self.env = Environment(`

`core/application/services/prompt/prompt_builder.py:96`
> `            loader=FileSystemLoader(str(self.template_dir)),`

`core/application/services/prompt/prompt_builder.py:100`
> `        )`

`core/application/services/prompt/prompt_builder.py:54`
> `            self._parts.append(self._env.get_template(template).render(**kwargs))`

`core/application/services/prompt/prompt_builder.py:66`
> `            self._parts.append(self._env.get_template(template).render(**kwargs))`

`core/application/services/prompt/prompt_builder.py:137`
> `        self._user_prompt = user_prompt`

`core/application/services/prompt/prompt_builder.py:138`
> `        self._domain_context = domain_context`

Recommended minimal fix: Treat `PromptBuilder.env` and template files as immutable for the duration of a run. If live prompt editing is required, add a prompt-file read/write lock and recreate/clear the environment cache after edits.

## 16. Prompt-Loader Cache Mutated After Startup

Finding: The query API can overwrite prompt template files while a run's `PromptBuilder` environment may have already loaded/cached templates. There is no cross-component invalidation or lock between API writes and runtime prompt rendering.

Severity: P2

Evidence:

`query/api/routes/prompts.py:76`
> `async def _write_file(path: Path, content: str) -> None:`

`query/api/routes/prompts.py:78`
> `    async with aiofiles.open(path, mode="w", encoding="utf-8") as f:`

`query/api/routes/prompts.py:181`
> `        env = Environment(autoescape=False)  # noqa: S701`

`query/api/routes/prompts.py:182`
> `        env.parse(update.content)`

`query/api/routes/prompts.py:190`
> `    await _write_file(path, update.content)`

`core/application/services/prompt/prompt_builder.py:95`
> `        self.env = Environment(`

`core/application/services/prompt/prompt_builder.py:54`
> `            self._parts.append(self._env.get_template(template).render(**kwargs))`

Recommended minimal fix: Make prompt updates atomic (`mkstemp` + `replace`) and coordinate them with running prompt builders. Minimal policy fix: disable prompt editing during active runs. Minimal technical fix: centralize prompt loading behind a service that locks writes and invalidates/recreates active environments.

## 17. Query/API Shared State During a Run

Finding: The query API shares an app-level event store pool and module-level bootstrap factories. Read-side route state is mostly per request, but prompt editing is a write path to shared template files. SSE uses per-request trackers plus the global `EventBroadcaster` singleton; the broadcaster's subscriber registry is locked.

Severity: P2

Evidence:

`query/api/app.py:27`
> `_event_store_factory: Callable[[str], EventStorePort] | None = None`

`query/api/app.py:37`
> `    global _event_store_factory`

`query/api/app.py:72`
> `    event_store = _event_store_factory(settings.database.connection_string)`

`query/api/app.py:76`
> `    app.state.event_store = event_store`

`query/api/routes/events.py:336`
> `        tracker = IncrementalHierarchyTracker(event_store, root_id)`

`query/api/routes/events.py:337`
> `        broadcaster = EventBroadcaster.get_instance()`

`query/api/routes/events.py:342`
> `        async with broadcaster.subscribe(root_id) as realtime_queue:`

`query/api/routes/prompts.py:78`
> `    async with aiofiles.open(path, mode="w", encoding="utf-8") as f:`

`infrastructure/adapters/postgres_event_store.py:139`
> `        self.pool = await asyncpg.create_pool(`

`infrastructure/adapters/postgres_event_store.py:172`
> `            async with self.pool.acquire() as conn:`

Recommended minimal fix: Keep query routes read-only during active runs unless there is a run coordinator. For prompt writes, add the lock/cache-invalidation fix from questions 15-16. For in-process API control of runs, explicitly attach and guard `app.state.execution_service`; `create_app` currently only attaches `event_store`.
