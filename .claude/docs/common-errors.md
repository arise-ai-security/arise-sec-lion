<!-- Read this when: debugging a failure, error, or unexpected behavior -->

Known failure modes with exact error messages, organized by category.

---

## Development-Time Errors

### Architecture Boundary Violation (Pre-Commit Hook)

```
Architecture boundary violations detected:
 - core/some/file.py:12: core must not import from infrastructure
```

If you see this, it's because `scripts/check_architecture_boundaries.py` scans `core/` for forbidden patterns:

| Pattern in `core/` | Message |
|---|---|
| `from infrastructure` / `import infrastructure` | core must not import from infrastructure |
| `from openhands` / `import openhands` | OpenHands SDK imports belong in infrastructure adapters |
| `CmdOutputMetadata(` | OpenHands SDK metadata parsing belongs in infrastructure adapters |
| `Observation: kind=` | OpenHands SDK repr parsing belongs in infrastructure adapters |

It also rejects `*.domain-fixture.*` files at the repo root -- those must live under `plugins/` or `tests/`.

Fix: Move the import to `infrastructure/adapters/`. If needed in `core/`, define a Protocol in `core/ports/` and implement in `infrastructure/`.

### Ruff Lint / Format Failures

If you see `ruff check failed` or `ruff format failed`, it's because pre-commit runs ruff with config from `ruff.toml` (Python 3.12, 100-char lines, 26 rule sets).

Fix: Run `ruff check --fix . && ruff format .`. Common issues: missing type annotations (ANN), unsorted imports (I, first-party: `core`, `infrastructure`, `config`), unused imports (F401). ANN and S101 are suppressed in test files.

### Pyright Type Errors

Fix: Run `pyright`. Common issues: missing return types, Protocol signatures not matching adapters, cross-layer imports needing `TYPE_CHECKING` blocks.

---

## Configuration & Environment Errors

### Missing POSTGRES_PASSWORD

```
ValueError: POSTGRES_PASSWORD environment variable is required
```

Source: `config/settings.py`, `Settings._build_from_config`. The only mandatory env var.

Fix: `export POSTGRES_PASSWORD=your_password`. Other DB env vars (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_DB`) are optional overrides with defaults from `config/config.yaml`.

### Wrong ARISE_ENV Value

No explicit error. The system silently loads only `config/config.yaml` without an overlay if `config/config.{ARISE_ENV}.yaml` does not exist.

Source: `config/settings.py`, `_load_yaml_hierarchy`. `ARISE_ENV` defaults to `development`. Valid values: `development`, `dev`, `production`.

Fix: Verify `config/config.{ARISE_ENV}.yaml` exists on disk.

### Config File Not Found (from_yaml)

```
FileNotFoundError: Config file not found: /path/to/config.yaml
```

Source: `config/settings.py`, `Settings.from_yaml`. Used primarily in tests.

Fix: Check the path. For normal startup use `Settings.load()`.

### Missing LLM API Keys

```
LLMError: LLM authentication failed for model 'gpt-4': ...
```

Source: `infrastructure/adapters/litellm_adapter.py`, `_call_litellm`.

Fix: Export the key for the configured provider:
```bash
export OPENAI_API_KEY=sk-...         # gpt-4, o3, o4
export ANTHROPIC_API_KEY=sk-ant-...  # claude
export GEMINI_API_KEY=...            # gemini
```
Check `boss.model`, `manager.model`, `worker.model` in `config/config.yaml`.

---

## Database / Event Store Errors

### Connection Pool Not Initialized

```
EventStoreError: Connection pool not initialized. Call connect() first.
```

Source: `infrastructure/adapters/postgres_event_store.py`. Every method checks `self.pool`.

Fix: Ensure bootstrap calls `event_store.connect()` before any operations. If pool is None, verify PostgreSQL is running and connection params are correct.

### OCC Concurrency Conflict

```
ConcurrencyError: Concurrency conflict for <aggregate_id>: expected v<N>
```

Source: `infrastructure/adapters/postgres_event_store.py`. The `UNIQUE(aggregate_id, sequence_number)` constraint triggers `asyncpg.UniqueViolationError`, wrapped as `ConcurrencyError`.

This is normal OCC behavior, handled automatically with retries (default 3). If it propagates to top level, all retries were exhausted.

### Unknown Event Type During Deserialization

```
EventStoreError: Unknown event type: <SomeEventType>
```

Source: `infrastructure/adapters/postgres_event_store.py`, `_deserialize_event`.

If you see this, it's because a new event class was added to `core/domain/events/events.py` but not registered in `EVENT_TYPE_REGISTRY`.

Fix: Add the event class to `EVENT_TYPE_REGISTRY` in `infrastructure/adapters/postgres_event_store.py`.

### Null Bytes in JSONB Insertion

```
asyncpg.exceptions.DataError: invalid input for query argument ... invalid byte sequence
```

Worker output (sanitizer reports, binary tool output) can contain `\x00` bytes that PostgreSQL JSONB rejects. The `_encode_jsonb` method in `postgres_event_store.py` strips these. If you see this error, a code path is bypassing that encoder.

Fix: Ensure data passes through `_encode_jsonb`. Primary fix is in commit 909bac0.

### Failed to Initialize Event Store Schema

```
EventStoreError: Failed to initialize event store schema
```

Source: `infrastructure/adapters/postgres_event_store.py`, `initialize_schema`. The SQL file `infrastructure/sql/create_events_table.sql` failed.

Fix: Verify PostgreSQL is running, database exists, and the user has CREATE TABLE permissions.

---

## LLM Errors

All LLM errors are wrapped as `LLMError` (defined in `core/domain/exceptions.py`, raised in `infrastructure/adapters/litellm_adapter.py`). Format: `"<message> (caused by: <original>)"`.

### LLM Rate Limit

```
LLMError: LLM rate limit exceeded for model '<model>': ...
```

Fix: Reduce `orchestration.concurrency.max_concurrent_llm_calls`. Add jitter via `llm_jitter_max_ms`. Configure `orchestration.retry.model_escalation_chain` for fallback.

### LLM Timeout

```
LLMError: LLM request timed out for model '<model>': ...
```

Fix: Increase `max_tokens` in the relevant config section or configure LiteLLM timeout.

### LLM Service Unavailable

```
LLMError: LLM service unavailable for model '<model>': ...
```

Fix: Wait and retry. Model escalation chain handles this automatically if configured.

### LLM API Error (Generic)

```
LLMError: LLM API error for model '<model>': ...
```

Fix: Check model name, `max_tokens`, `temperature`. O-series models (`o1`, `o3`, `o4`) reject temperature -- handled automatically by `_is_o_series()`.

### LLM Returned Empty Response

```
LLMError: LLM returned empty response
```

Source: `litellm_adapter.py`, `query` and `query_with_usage`. The LLM returned `None` content.

Fix: Check the prompt. Enable DEBUG logging. Triggers retry via escalation chain if configured.

### Circuit Breaker Tripped

**Behavior:** A model silently stops receiving requests. Retry policy skips to next model in chain.

Source: `core/application/services/orchestration/retry_policy.py`, `_is_circuit_broken`.

If you see retries stopping early, it's because a model accumulated `circuit_breaker_threshold` (default 3) consecutive failures.

Fix: Resets between runs. Adjust `orchestration.retry.circuit_breaker_threshold`. Populate `orchestration.retry.model_escalation_chain` for fallbacks.

---

## Orchestration Runtime Errors

### Max Run Duration Exceeded

```
System loop timed out for root <root_agent_id> after <N> seconds
```

Source: `core/application/execution_service.py`, `_handle_run_timeout`. In-flight tasks are cancelled, remaining agents are failed with `"Run timed out after <N> seconds"`, run status becomes `timed_out`.

Fix: Increase `orchestration.max_run_duration_seconds` (default 1800). Or reduce scope via topology limits.

### InfeasibleError / Constraints Unsatisfiable

```
Constraints unsatisfiable: <reason> (needs at least <N> subtasks)
```

Source: `core/domain/services/subtask_parser.py`. When the LLM responds with `"status": "constraints_unsatisfiable"`, an `InfeasibleError` is raised. The agent is marked infeasible via `DecisionInfeasible` event. Parent re-decomposes up to `max_redecompositions` (default 2) times.

Fix: Relax `topology.max_depth`, `topology.max_children_per_node`, or `topology.max_total_agents`.

### Topology Limit Violations

```
LLM violated limits: ['children']
LLM violated limits: ['total_agents']
```

Source: `core/application/agent_orchestrator.py`. The LLM produced more subtasks than limits allow. A `LimitEnforced` event is emitted.

Fix: Increase the relevant limits in `orchestration.topology`.

### Assessment Parse Failure

```
Assessment failed after 2 attempts: <JSONDecodeError or ValueError>
```

Source: `core/application/agent_orchestrator.py`, `assess_task`. The LLM returned non-JSON or invalid action. Retries up to 2 times. Valid actions: `"execute"`, `"decompose"`.

Fix: Check prompt template `prompts/operations/assess.j2`. Enable DEBUG logging. Consider a more capable model.

### Subtask Parse Failures

```
ValueError: LLM response is not valid JSON: ...
ValueError: Empty subtask list
ValueError: Subtask <N>: missing 'config' field
ValueError: Subtask <N>: invalid config: ...
```

Source: `core/domain/services/subtask_parser.py`. The parser handles several JSON formats and sanitizes LLM hallucinations (invalid tool names logged as warnings, non-integer `depends_on`). Valid tools: `claude_code`, `openhands`, `google_adk`.

Fix: Check the decomposition prompt and raw LLM response.

### Worker Tool Not Available

```
ToolNotAvailableError: Tool '<tool_name>' unavailable. Available: [...]
```

Source: `core/domain/exceptions.py`, raised in `core/application/agent_orchestrator.py`.

Fix: Set `worker.tool` to `claude_code`, `openhands`, or `google_adk` in config. Ensure the SDK is installed.

### Token Budget Exceeded

```
Token budget exceeded (~<N> tokens), forcing final answer
```

Source: `core/application/services/toolset/tool_calling_service.py`. Estimated tokens exceeded `orchestration.tool_calling.token_budget` (default 80000).

Fix: Increase `token_budget` or lower `result_char_limit` in config.

### Tool-Calling Max Iterations

```
Tool-calling loop hit max iterations (<N>), forcing final answer
```

Source: `core/application/services/toolset/tool_calling_service.py`. Agent exhausted iteration budget.

Fix: Increase `orchestration.tool_calling.max_iterations` or per-role `max_iterations` in `orchestration.tool_calling.policies`.

### Verification Failed

**Behavior:** Completed worker rolled back to failed. `VerificationFailed` event emitted with `failed_stage` and `feedback`.

Source: `core/application/services/orchestration/verification_pipeline.py`. 4 stages:
1. **structural** -- `"Worker produced empty or blank output"`
2. **deterministic** -- placeholder, always passes
3. **execution** -- placeholder, always passes
4. **judge** -- LLM evaluates against `success_criteria`

Fix: If structural fails, check worker result content. If judge is too strict, set `orchestration.skip_judge: true` (stages 1-3 still run). Unparseable judge responses pass silently.

### Agent Not Found

```
AgentNotFoundError: <agent_id>
```

Source: `core/application/services/lifecycle/agent_repository.py`, `load`. No events exist for the aggregate_id.

Fix: Check the `events` table. Usually indicates a lifecycle bug or race condition during spawning.

---

## Docker / SEC-bench Errors

### Missing SEC-bench Docker Image

```
RuntimeError: Missing SEC-bench image <image>. Build it first with deployment/build-secbench-tools.sh.
```

Source: `plugins/security/docker_runtime.py`, `_ensure_image_exists`.

Fix: `./deployment/build-secbench-tools.sh deployment/<instance>.json`

### HOST_PROJECT_ROOT Not Set (DooD)

**Behavior:** Volume mounts map to wrong paths. Container cannot access source/testcase files.

Source: `plugins/security/docker_runtime.py`, `_host_path`. Only needed when the orchestrator runs inside a Docker container with host Docker socket mounted.

Fix: `export HOST_PROJECT_ROOT=/absolute/path/to/project/on/host`

### Docker Socket Permission Denied

```
permission denied while trying to connect to the Docker daemon socket
```

Fix: `sudo usermod -aG docker $USER` and re-login. On macOS, ensure Docker Desktop is running.

### Unsupported SEC-bench work_dir

```
RuntimeError: Unsupported SEC-bench work_dir outside /src: <work_dir>
```

Source: `plugins/security/docker_runtime.py`, `_map_host_work_dir`. The `work_dir` must be `/src` or start with `/src/`.

Fix: Check the CVE instance JSON file's `work_dir` field.

### CVE Instance Inference Failure

```
CVEInferenceError: Cannot infer CVE instance. Missing: <fields>. Provide --cve-file for explicit SEC-bench configuration, or include GitHub repo URL and 40-char commit hash in task description.
```

Source: `plugins/security/cve_inference.py`.

Fix: Provide `--cve-file deployment/<instance>.json`, or include a GitHub repo URL and 40-char commit hash in the task text.

---

## Domain Model Errors

### DomainInvariantError

```
DomainInvariantError: Cannot spawn BOSS child
DomainInvariantError: Requires BOSS/MANAGER, got worker
DomainInvariantError: Requires WAITING status, got analyzing
DomainInvariantError: child_id <id> not in spawned children
```

Source: `core/domain/aggregates/agent_session.py`. Hard invariants on the `AgentSession` aggregate.

Fix: Bug in orchestration logic. Check agent status and role before the operation.

### InvalidEventHistoryError

```
InvalidEventHistoryError: Cannot load from empty event history
InvalidEventHistoryError: First event must be AgentCreated, got <EventType>
```

Source: `core/domain/aggregates/agent_session.py`, `load_from_history`. Event stream is corrupt.

Fix: Check the `events` table. Ensure events start from sequence 1 with `AgentCreated` first.

### Unregistered Event Apply Handler

```
TypeError: No handler for <EventType>. Register with @_apply.register.
```

Source: `core/domain/aggregates/agent_session.py`, `_apply` singledispatch fallback. A new event type has no replay handler.

Fix: Add a `@_apply.register` method on `AgentSession` for the event type.

### CostInvariantViolation

```
Cost invariant violated: <invariant>
  Expected: <expected>
  Actual: <actual>
```

Source: `core/domain/exceptions.py`. Bug in cost calculation. Should never occur if tests pass.

Fix: File a bug with the full error message.

---

## Template Errors

### Jinja2 TemplateNotFound

```
jinja2.exceptions.TemplateNotFound: Required <context> not found: <template>
```

Source: `core/application/services/prompt/prompt_builder.py`, `TemplateChain.render`.

Template hierarchy under `prompts/`:
- `system.j2` (tier 1)
- `roles/*.j2` (tier 2: boss, manager, worker, pending)
- `operations/*.j2` (tier 3: assess, decomposition, execution)
- `context/*.j2` (tier 4: scope, sibling)
- `domains/secbench/*.j2` (tier 4: domain-specific)

Fix: Check the named template exists under `prompts/`. Note: `render_optional` silently skips missing templates; only `render` raises.

---

## Query / Projection Errors

### RegistryError

```
RegistryError: Unknown <component_type>: '<name>'. Available: <list>
```

Source: `core/query/projections/registry.py`. A pipeline query references an unregistered filter, formatter, or sink.

Fix: Check the component name against registered components.
