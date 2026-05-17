# Concurrency Audit 4: Docker / DooD Worker Runtime

Scope: Docker / Docker-out-of-Docker behavior when 10 `main.py run` subprocesses execute in parallel from `experiments/shared/scripts/run_matrix.py`.

I did not run Docker commands or tests. I read the requested source files and supporting call sites. Findings below distinguish direct observations from inferences based on missing cleanup/retry/locking hooks.

## 1. Container Naming

Finding: Container names are `secbench-worker-<root_id_hex8>-<agent_id_hex8>`. `root_id` is generated with `uuid4()` per run, and the run directory uses the full UUID, but Docker names only use the first 8 hex chars of each UUID. In flat mode `agent_id == root_id`, so the name effectively carries only one 32-bit prefix twice. In hierarchical mode the name carries two 32-bit prefixes. This is usually unique for 10 parallel runs, but not collision-proof over many historical runs.

Severity: P1

Evidence:

plugins/security/docker_runtime.py:83 >         container_name = (
plugins/security/docker_runtime.py:84 >             f"{self._container_prefix}-{workspace.root_id.hex[:8]}-{agent_id.hex[:8]}"
plugins/security/docker_runtime.py:85 >         )
core/application/execution_service.py:230 >         root_id = uuid4()
core/application/execution_service.py:753 >         run_output_path = base_output / str(root_id)
core/application/execution_service.py:754 >         run_output_path.mkdir(parents=True, exist_ok=True)

Recommended fix: Use the full UUIDs, or at least 16 hex chars, in `container_name`. Prefer `secbench-worker-{root_id.hex}-{agent_id.hex}` and keep labels for lookup. This is the smallest fix that avoids short-prefix birthday collisions and stale-container name collisions.

## 2. Lazy-Start Race Within One Run

Finding: The check-and-start path is serialized by a per-`root_id` `asyncio.Lock`. This prevents two concurrent callers from both running `docker run` for the same run. The second caller does not reuse the active session; it raises `RuntimeError` if one already exists. I observed no DB row or class-level cross-process guard; the lock is per plugin instance inside one subprocess.

Severity: P1

Evidence:

plugins/security/plugin.py:64 >         self._session_locks: dict[UUID, asyncio.Lock] = {}
plugins/security/plugin.py:194 >         lock = self._session_locks.setdefault(root_id, asyncio.Lock())
plugins/security/plugin.py:195 >         async with lock:
plugins/security/plugin.py:196 >             if root_id in self._sessions:
plugins/security/plugin.py:197 >                 raise RuntimeError(
plugins/security/plugin.py:198 >                     f"SEC-bench container already active for run {root_id}"
plugins/security/plugin.py:201 >             session = await self._container_runtime.start_session(
plugins/security/plugin.py:206 >             self._sessions[root_id] = session

Recommended fix: Keep the lock. If same-run multi-worker execution is ever allowed, change the locked block to return/reuse the existing session with an exec lock, or maintain a per-worker container model. Raising is safe only while workers are truly sequential.

## 3. Cleanup On Success

Finding: Normal hierarchical worker execution cleans up in a `finally` block; normal flat-mode success cleans up after run completion. Plugin cleanup pops `_sessions[root_id]` and calls `stop_session`, which runs `docker rm -f`.

Severity: P1

Evidence:

core/application/services/lifecycle/role_dispatch.py:116 >             try:
core/application/services/lifecycle/role_dispatch.py:117 >                 await self._context.orchestrator.execute_task(
core/application/services/lifecycle/role_dispatch.py:126 >             finally:
core/application/services/lifecycle/role_dispatch.py:127 >                 await self._cleanup_worker_context(
core/application/services/lifecycle/role_dispatch.py:164 >         await self._context.domain_plugin.cleanup_worker_execution(
core/application/execution_service.py:485 >         if self._domain_plugin is not None:
core/application/execution_service.py:486 >             await self._domain_plugin.cleanup_worker_execution(
plugins/security/plugin.py:231 >         session = self._sessions.pop(root_id, None)
plugins/security/plugin.py:236 >             await self._container_runtime.stop_session(session)
plugins/security/docker_runtime.py:145 >     async def stop_session(self, session: SecBenchContainerSession) -> None:
plugins/security/docker_runtime.py:147 >         await self._run_best_effort(["docker", "rm", "-f", session.container_id])

Recommended fix: Move flat-mode cleanup into a `finally` around the whole post-`prepare_worker_execution` block. The hierarchical path already has the correct shape.

## 4. Cleanup On Failure / Abort

Finding: Ordinary worker exceptions are covered in hierarchical mode and in the flat-mode `_flat_worker.run_task` exception block. Process death is not covered. I found no `atexit` handler and no signal handler that removes worker containers. `KeyboardInterrupt` at the top level prints a message and exits, but does not call domain cleanup. Because `docker run` is detached and the command is `tail -f /dev/null`, containers can remain running after the Python subprocess dies. This matches the prior observed zombies.

Severity: P1

Evidence:

plugins/security/docker_runtime.py:87 >             "docker",
plugins/security/docker_runtime.py:88 >             "run",
plugins/security/docker_runtime.py:89 >             "-d",
plugins/security/docker_runtime.py:110 >             workspace.image,
plugins/security/docker_runtime.py:111 >             "tail",
plugins/security/docker_runtime.py:112 >             "-f",
plugins/security/docker_runtime.py:113 >             "/dev/null",
plugins/security/docker_runtime.py:117 >         # Audit BUG-B: the container is already running after `docker run`;
plugins/security/docker_runtime.py:123 >         try:
plugins/security/docker_runtime.py:132 >         except Exception:
plugins/security/docker_runtime.py:133 >             await self._run_best_effort(["docker", "rm", "-f", container_id])
bootstrap/bootstrap.py:54 >     try:
bootstrap/bootstrap.py:55 >         asyncio.run(handlers[parsed.command](parsed))
bootstrap/bootstrap.py:56 >     except KeyboardInterrupt:
bootstrap/bootstrap.py:57 >         print("\nInterrupted. Agent state is persisted in the event store.")
agent-docs/parallel-20-host-saturation-diagnosis.md:56 > `docker ps -a` showed:
agent-docs/parallel-20-host-saturation-diagnosis.md:58 > - 3 secbench-worker containers **Up 45 minutes** with 0.00% CPU and 2.844 MiB RSS — zombies from a prior failed run
agent-docs/parallel-20-host-saturation-diagnosis.md:59 > - ~15 historical secbench-worker containers in `Exited (137)` (= SIGKILL, usually OOM kill or `docker rm -f`) over the past 53 minutes to 2 hours

Recommended fix: Add a run-scoped cleanup owner that removes containers by labels `arise.root_id`, `arise.agent_id`, and a run-owner PID/session label. Register cleanup on normal exit, `SIGINT`, and `SIGTERM`. Also provide a pre-batch cleanup command that removes `secbench-worker` containers with stale labels.

## 5. Stale-Container Collision

Finding: If a stale container exists with the exact generated name, `docker run --name <same>` fails before a session is returned. `_run_checked` raises the Docker stderr as `RuntimeError`; there is no stale-name recovery path. The current post-run setup cleanup only applies after `docker run` succeeds and returns a `container_id`.

Severity: P1

Evidence:

plugins/security/docker_runtime.py:92 >             "--name",
plugins/security/docker_runtime.py:93 >             container_name,
plugins/security/docker_runtime.py:115 >         container_id = (await self._run_checked(cmd)).strip()[:12]
plugins/security/docker_runtime.py:123 >         try:
plugins/security/docker_runtime.py:132 >         except Exception:
plugins/security/docker_runtime.py:133 >             await self._run_best_effort(["docker", "rm", "-f", container_id])
plugins/security/docker_runtime.py:262 >     async def _run_checked(self, cmd: list[str]) -> str:
plugins/security/docker_runtime.py:263 >         exit_code, stdout, stderr = await self._run_command(cmd)
plugins/security/docker_runtime.py:264 >         if exit_code != 0:
plugins/security/docker_runtime.py:265 >             raise RuntimeError(stderr.strip() or stdout.strip() or "Command failed")

Recommended fix: Use full-UUID names to make collision practically irrelevant, and before creating a new session, remove only containers whose labels match the exact `root_id` and are known stale. Do not blindly remove a name that may belong to another live run.

## 6. DooD Socket Contention And Retries

Finding: Docker daemon concurrency is generally safe, but this code does not retry transient Docker CLI/socket failures. I observed single-shot calls in `DockerSecBenchRuntime._run_command`, `secb-exec`, `ContainerSessionContext.wrap_shell_command`, the MCP helper path, and `ClaudeCodeWorker._wrap_with_docker_exec`.

Severity: P1

Evidence:

plugins/security/docker_runtime.py:273 >     async def _run_command(self, cmd: list[str]) -> tuple[int, str, str]:
plugins/security/docker_runtime.py:274 >         process = await asyncio.create_subprocess_exec(
plugins/security/docker_runtime.py:280 >         stdout, stderr = await process.communicate()
plugins/security/docker_runtime.py:281 >         return (
plugins/security/docker_runtime.py:248 >                     'if docker exec "$CONTAINER" test -d "$WORKDIR" 2>/dev/null; then',
plugins/security/docker_runtime.py:249 >                     '  docker exec -i -w "$WORKDIR" "$CONTAINER" bash -lc "$CMD"',
infrastructure/adapters/worker/shared/container_session.py:166 >             "docker exec -i "
infrastructure/adapters/worker/shared/container_session.py:167 >             f"{shlex.quote(self.container_id)} "
plugins/security/mcp/security_tools_server.py:120 >     argv = ["bash", "-lc", command] if helper_script is None else [helper_script, command]
plugins/security/mcp/security_tools_server.py:122 >         completed = subprocess.run(  # noqa: S603 - argv built from trusted env / sanitized command
infrastructure/workers/claude_code_worker.py:424 >         wrapped: list[str] = ["docker", "exec", "-i"]

Recommended fix: Centralize Docker CLI calls behind a small retry wrapper with exponential backoff and jitter for transient errors such as daemon socket reset, timeout, or temporary unavailability. Do not retry deterministic errors such as missing images, permission denied, or container-name conflict.

## 7. Docker Exec Ordering Inside One Run

Finding: Worker scheduling currently enforces one eligible worker at a time for a run by passing `sequential_workers=True`, and `AgentQueryService` returns only the leftmost eligible worker. However, I found no container-level lock around `docker exec` itself. If a future path allows two worker executions, or an agent/tool server issues concurrent helper calls, the same container workspace can be mutated concurrently.

Severity: P1

Evidence:

core/application/execution_service.py:315 >             active_agents = await self._query_service.get_active_agent_ids(
core/application/execution_service.py:316 >                 root_id=root_agent_id, sequential_workers=True
core/application/services/query/query_service.py:218 >     async def get_active_agent_ids(
core/application/services/query/query_service.py:221 >         sequential_workers: bool = False,
core/application/services/query/query_service.py:228 >             sequential_workers: If True, only return one WORKER at a time in
core/application/services/query/query_service.py:278 >         if not sequential_workers:
core/application/services/query/query_service.py:314 >         # Sort by path and return only the leftmost eligible worker
core/application/services/query/query_service.py:316 >         return [*non_workers, eligible_workers[0][0]]
plugins/security/docker_runtime.py:248 >                     'if docker exec "$CONTAINER" test -d "$WORKDIR" 2>/dev/null; then',
plugins/security/docker_runtime.py:249 >                     '  docker exec -i -w "$WORKDIR" "$CONTAINER" bash -lc "$CMD"',
infrastructure/adapters/worker/shared/container_session.py:166 >             "docker exec -i "

Recommended fix: Keep scheduler-level sequential workers, and add a per-run exec mutex for the helper paths. For the shell helper, use a lock file under `workspace.host_root` with `flock` where available; for Python-generated `docker exec`, use an `asyncio.Lock` keyed by `container_id`.

## 8. Mount-Time Races

Finding: Worker mounts are per-run subdirectories: `src`, `testcase`, and the workspace root under `run_output_path`, which is `output_directory / str(root_id)`. The parent `runs/` directory and the repo are shared into the app container, but each worker container gets a unique run subtree. I observed no cross-run shared `src` or `testcase` mount in the Docker runtime.

Severity: P2

Evidence:

core/application/execution_service.py:753 >         run_output_path = base_output / str(root_id)
plugins/security/docker_runtime.py:47 >         source_dir = run_output_path / "src"
plugins/security/docker_runtime.py:48 >         testcase_dir = run_output_path / "testcase"
plugins/security/docker_runtime.py:49 >         helper_script = run_output_path / "secb-exec"
plugins/security/docker_runtime.py:100 >             "-v",
plugins/security/docker_runtime.py:101 >             f"{self._host_path(workspace.host_source_dir)}:{workspace.container_source_dir}",
plugins/security/docker_runtime.py:102 >             "-v",
plugins/security/docker_runtime.py:103 >             f"{self._host_path(workspace.host_testcase_dir)}:{workspace.container_testcase_dir}",
plugins/security/docker_runtime.py:108 >             "-v",
plugins/security/docker_runtime.py:109 >             f"{self._host_path(workspace.host_root)}:{workspace.container_workspace_root}",
deployment/docker-compose.yml:45 >       - ..:/app
deployment/docker-compose.yml:48 >       - ../runs:/app/runs  # Host bind mount for SEC-bench artifacts (DooD needs host paths)

Recommended fix: Keep full-UUID run directories. Add a startup assertion that `workspace.host_root`, `host_source_dir`, and `host_testcase_dir` all resolve under the expected output root before passing them to Docker.

## 9. No CPU / Memory Limits

Finding: `docker run` has no `--cpus`, `--memory`, or `--memory-swap` flags, and `SecurityConfig` has no resource-budget fields. Ten containers can compete unbounded with 10 host subprocesses and the app/db/web containers. This is a performance and flakiness risk, not direct container corruption.

Severity: P2

Evidence:

plugins/security/docker_runtime.py:86 >         cmd = [
plugins/security/docker_runtime.py:87 >             "docker",
plugins/security/docker_runtime.py:88 >             "run",
plugins/security/docker_runtime.py:89 >             "-d",
plugins/security/docker_runtime.py:90 >             "--network",
plugins/security/docker_runtime.py:91 >             "host",
plugins/security/docker_runtime.py:110 >             workspace.image,
config/settings.py:395 > class SecurityConfig(BaseModel):
config/settings.py:400 >     enabled: bool = True  # Uses worker.timeout for secb commands
config/settings.py:401 >     tools: list[str] = Field(
agent-docs/parallel-20-host-saturation-diagnosis.md:31 > | `plugins/security/docker_runtime.py:80-143` | `docker run` is invoked WITHOUT `--memory` or `--cpus` — containers compete unbounded |
agent-docs/parallel-20-host-saturation-diagnosis.md:182 > **Load avg 71 on a 10-core machine = 7.1× CPU count.** Sustained.

Recommended fix: Add optional config fields, for example `security.worker_cpus`, `security.worker_memory`, and `security.worker_memory_swap`. For a 10-core / 32 GB host running 10 parallel runs, a conservative default is `--cpus=1`, `--memory=2g`, `--memory-swap=2g`, plus a matrix default of `--parallel 5-8` for Valgrind/KLEE-heavy jobs if caps cause false timeouts.

## 10. Network Mode

Finding: Worker containers use `--network host`. This avoids Docker bridge isolation and makes all worker processes share the host network namespace. I observed no port publishing in `docker run`, but if benchmark commands inside two containers bind the same fixed port, they can collide across runs.

Severity: P1

Evidence:

plugins/security/docker_runtime.py:90 >             "--network",
plugins/security/docker_runtime.py:91 >             "host",
agent-docs/parallel-20-host-saturation-diagnosis.md:231 > | H5 | `--network host` port collision | **REFUTED** | Containers don't bind ports (all docker exec'd into) |

Recommended fix: Use Docker's default bridge network unless a specific SEC-bench task requires host networking. If host networking is needed, make it opt-in per CVE/image and allocate per-run ports through environment variables.

## 11. HOST_PROJECT_ROOT Env Semantics

Finding: DooD path mapping depends on `HOST_PROJECT_ROOT`. The code accepts the variable without validating that it is absolute or correct. Compose defaults it to `.` when missing. In the local profile, `/app/runs` is a bind mount from `../runs`, but in the dev profile it is a named Docker volume; a host daemon cannot bind-mount a named volume's in-container path into another host-created container by using `/app/runs` path replacement. If the variable is wrong or missing, all parallel runs fail or mount the wrong host path pattern.

Severity: P1

Evidence:

plugins/security/docker_runtime.py:25 >         # DooD path mapping: container /app → host project root.
plugins/security/docker_runtime.py:29 >         self._host_project_root = os.environ.get("HOST_PROJECT_ROOT", "")
plugins/security/docker_runtime.py:34 >         if self._host_project_root and resolved.startswith("/app/"):
plugins/security/docker_runtime.py:35 >             return resolved.replace("/app/", self._host_project_root + "/", 1)
deployment/docker-compose.yml:47 >       - /var/run/docker.sock:/var/run/docker.sock
deployment/docker-compose.yml:48 >       - ../runs:/app/runs  # Host bind mount for SEC-bench artifacts (DooD needs host paths)
deployment/docker-compose.yml:55 >       DOCKER_HOST: unix:///var/run/docker.sock  # Enable DooD for SEC-bench workers
deployment/docker-compose.yml:56 >       HOST_PROJECT_ROOT: ${HOST_PROJECT_ROOT:-.}  # Host path for DooD volume mapping
deployment/docker-compose.yml:136 >       - /var/run/docker.sock:/var/run/docker.sock
deployment/docker-compose.yml:138 >       - secbench_output:/app/runs  # Shared volume for SEC-bench artifacts (PoC, patches, logs)
deployment/docker-compose.yml:141 >       - DOCKER_HOST=unix:///var/run/docker.sock  # Enable DooD for SEC-bench workers
deployment/docker-compose.yml:142 >       - HOST_PROJECT_ROOT=${HOST_PROJECT_ROOT:-.}  # Host path for DooD volume mapping

Recommended fix: Fail fast when running with Docker socket access and `HOST_PROJECT_ROOT` is unset, relative, or does not contain the repo and `runs/` directory. Remove the `:-.` default. Do not use a named volume for `/app/runs` in DooD mode unless the worker container is attached to that same Docker volume by name instead of host path.

## 12. secb-exec Helper Script

Finding: `secb-exec` is a host-side script written into each run's workspace root, not a shared global script. Across runs it is isolated by full `root_id` run directories. It has no locking, so concurrent helper invocations inside the same run can race on the same container filesystem, but two different runs do not share the same helper file or mounted source/testcase directories.

Severity: P2

Evidence:

plugins/security/docker_runtime.py:47 >         source_dir = run_output_path / "src"
plugins/security/docker_runtime.py:48 >         testcase_dir = run_output_path / "testcase"
plugins/security/docker_runtime.py:49 >         helper_script = run_output_path / "secb-exec"
plugins/security/docker_runtime.py:225 >     def _write_exec_helper(self, session: SecBenchContainerSession) -> None:
plugins/security/docker_runtime.py:226 >         helper = session.workspace.helper_script
plugins/security/docker_runtime.py:229 >         helper.write_text(
plugins/security/docker_runtime.py:240 >                     f"WORKDIR={shlex.quote(work_dir)}",
plugins/security/docker_runtime.py:241 >                     f"CONTAINER={shlex.quote(container)}",
plugins/security/docker_runtime.py:248 >                     'if docker exec "$CONTAINER" test -d "$WORKDIR" 2>/dev/null; then',
plugins/security/docker_runtime.py:249 >                     '  docker exec -i -w "$WORKDIR" "$CONTAINER" bash -lc "$CMD"',
infrastructure/adapters/worker/shared/container_session.py:69 >         helper_name = self.helper_script.name
infrastructure/adapters/worker/shared/container_session.py:105 >             f"**Shell commands — ALWAYS use the helper script `./{helper_name}` "

Recommended fix: Keep it per-run. Add per-run `flock` around the `docker exec` body if multiple helper calls can happen concurrently. For OpenHands, prefer a tool-level forced container shell instead of relying on prompt compliance.

## Shared vs Not Shared Summary

| Resource | Shared across runs? | Evidence | Risk |
|---|---:|---|---|
| Container names | No by intent, but short-prefix collisions possible | `plugins/security/docker_runtime.py:84` | P1 stale/collision failure |
| `runs/` host path | Parent shared; run subdirs unique | `core/application/execution_service.py:753`, `deployment/docker-compose.yml:48` | P1 if `HOST_PROJECT_ROOT` wrong; otherwise low |
| Repo host path | Shared into app, not directly mounted wholesale into worker | `deployment/docker-compose.yml:45`, `plugins/security/docker_runtime.py:100-109` | Low for workers; app-level shared repo edits still external |
| `/tmp` | Not mounted into worker by Docker runtime; some host-mode temp dirs exist | `infrastructure/workers/claude_code_worker.py:156`, `infrastructure/workers/claude_code_worker.py:228` | Low cross-run Docker risk |
| `/var/run/docker.sock` | Yes | `deployment/docker-compose.yml:47`, `deployment/docker-compose.yml:136` | P1 transient Docker CLI/socket failures; host daemon is shared |
| Network namespace | Yes, host network | `plugins/security/docker_runtime.py:90-91` | P1 port collisions if tests bind fixed ports |
| CPU/RAM budgets | Shared/unbounded | `plugins/security/docker_runtime.py:86-110`, `config/settings.py:395-404` | P2 host saturation |
| `HOST_PROJECT_ROOT` | One env value inherited by all runs in a shell/container | `plugins/security/docker_runtime.py:29-35`, `deployment/docker-compose.yml:56` | P1 wrong bind mounts for all parallel runs |
| `secb-exec` script location | Not shared across runs; per `run_output_path` | `plugins/security/docker_runtime.py:49`, `plugins/security/docker_runtime.py:225-226` | P2 no same-run exec lock |
