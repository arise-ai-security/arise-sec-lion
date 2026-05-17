# Concurrency Audit 2 — Infrastructure Adapters (LLM, Workers, Shared Clients)

Scope: `infrastructure/adapters/litellm_adapter.py`, `infrastructure/adapters/worker/*`,
`infrastructure/adapters/llm_format_repairer.py`,
`infrastructure/adapters/cost_calculator.py`, `infrastructure/workers/*`,
`infrastructure/snapshot.py`. Read-only. References quoted from working tree
(uncommitted changes included where relevant).

Concurrency model assumed (given facts):
- 10 OS subprocesses ("runs"), one container per run.
- Inside one run: managers run concurrently as asyncio tasks; workers run
  sequentially inside one container per run.
- Shared host resources across the 10 runs: docker daemon, Postgres, API keys.

A single `LiteLLMAdapter` is instantiated per run by `bootstrap/infrastructure.py:100`:

```
llm_adapter = LiteLLMAdapter()
```

It is shared by every manager and by the `LLMFormatRepairer` in the same process
(`bootstrap/infrastructure.py:107-112`). A single `OpenHandsAdapter` is similarly
created per run by `bootstrap/composition.py:136`. All concurrent calls in one
run hit those two shared instances.

---

## LiteLLM adapter — concurrent calls from one run

### 1. Shared module state in `litellm` — adapter does NOT mutate it

**Finding**: No code under `infrastructure/adapters/` or `bootstrap/` assigns to
`litellm.success_callback`, `litellm.failure_callback`, `litellm.callbacks`,
`litellm.cache`, `litellm.set_verbose`, `litellm.api_base`, `litellm.api_key`,
`litellm.drop_params`, or any other module-level `litellm` attribute. Grep:

```
grep -rn "litellm\.\(set_verbose\|api_base\|callback\|cache\|...\)" .
→ (no matches)
```

The only references to module-level `litellm` are read-only:
`litellm.acompletion(...)`, `litellm.exceptions.<Class>`, `litellm.completion_cost(...)`,
`litellm.get_model_cost_map(url="")`. So managers in the same run cannot stomp
each other through this codebase. LiteLLM itself maintains some module-level
state (caches, header dicts) — but that is library-internal and uses LiteLLM's
own locks; it is out of our scope to fix.

**Severity**: P3 (informational — keep it this way).

**Evidence**: `infrastructure/adapters/litellm_adapter.py:15` imports `litellm`
but never assigns to it. `infrastructure/adapters/litellm_adapter.py:132` calls
`litellm.acompletion(model=model, **kwargs)` with per-call kwargs only.

**Fix**: None required. Add a comment if drift is a concern.

---

### 2. Cost calculator concurrency

**Finding**: `LiteLLMAdapter._cost_calculator` is read-only after construction
and is invoked synchronously on the response path:

```python
# litellm_adapter.py:377-387
if self._cost_calculator is not None:
    return self._cost_calculator.calculate_llm_cost(model, prompt_tokens, completion_tokens)
try:
    cost = litellm.completion_cost(completion_response=response)
    return round(cost, 6)
except Exception:
    return 0.0
```

`DefaultCostCalculator.calculate_llm_cost` (`cost_calculator.py:199-216`) is
pure: it looks up pricing via the LRU-cached `_get_cached_model_cost_map`
(`cost_calculator.py:110-125`). `functools.lru_cache` is thread-safe in CPython.
The first call into `litellm.get_model_cost_map(url="")` reads the bundled
JSON from disk; LiteLLM serializes this internally.

The calculator is invoked synchronously inside the coroutine path
(`_extract_usage` at `litellm_adapter.py:186-205`). Pricing lookup is O(1) post
warmup, so it does not meaningfully block the loop.

**Severity**: P3.

**Fix**: None required.

---

### 3. Streaming responses — N/A

**Finding**: The adapter never sets `stream=True`. There is no `async for chunk
in response` anywhere in `litellm_adapter.py`. All three entrypoints (`query`,
`query_with_usage`, `query_with_tools`) call `_call_litellm` which awaits
`litellm.acompletion(...)` once and reads `response.choices[0].message`
(`litellm_adapter.py:235, 268, 322`).

**Severity**: N/A.

**Fix**: None required.

---

### 4. JSON repair / output-format repairer — amplification surface

**Finding**: When subtask/JSON parsing fails, the orchestrator calls
`LLMFormatRepairer.repair` (`infrastructure/adapters/llm_format_repairer.py:84-103`),
which routes through the SAME `LiteLLMAdapter` instance:

```python
# bootstrap/infrastructure.py:100-112
llm_adapter = LiteLLMAdapter()
...
if config.format_repairer_enabled:
    format_repairer = LLMFormatRepairer(
        llm_port=llm_adapter,
        model=config.format_repairer_model,
        ...
    )
```

The repairer itself is "one shot, no internal retries" per its module docstring
(`llm_format_repairer.py:21-22`), and on `LLMError` it returns the raw text
unchanged (lines 96-103). So a single parse failure produces at most one extra
LLM call per failing manager.

Amplification arises from CALL VOLUME, not recursion: when many concurrent
managers in one run all parse-fail simultaneously (common with the qwen/ollama
default per `litellm_adapter.py:79-103` comments), each launches an extra LLM
call through the same adapter. With 10 parallel runs and N managers per run,
this can briefly double LLM traffic on the same provider/key. Combined with the
no-jitter retry (finding #12), this is the cross-process burst surface.

**Severity**: P1 — not corruption, but a load-amplification path that
compounds with #12.

**Evidence**: `infrastructure/adapters/llm_format_repairer.py:84-103`;
`bootstrap/infrastructure.py:100-112`;
`infrastructure/adapters/litellm_adapter.py:105-184` (retry loop is per-call).

**Fix**: Either route the repairer through a separate `LiteLLMAdapter` instance
configured with a stricter retry budget, or bound concurrent repairer calls
with an `asyncio.Semaphore`. The current code does neither.

---

## OpenHands worker adapter — sequential workers in one container

### 5. `ThreadPoolExecutor(max_workers=1)` per worker

**Finding**: Created per `_execute_task` invocation (per worker), shut down in
the `finally` block:

```python
# openhands_adapter.py:157-159
executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="openhands"
)
try:
    ...
finally:
    self._request_shutdown(conversation)
    executor.shutdown(wait=False)   # line 215
```

Each worker gets its own executor (scoped to one `_execute_task` call). The
single `OpenHandsAdapter` instance shared across workers in one run holds NO
executor state. `wait=False` is acceptable in the happy path because the future
has already been awaited (`asyncio.wait_for(asyncio.wrap_future(future), ...)`
at line 165). On timeout, the thread may outlive the executor reference — the
test `test_timeout_logs_warning_when_thread_outlives_grace_period`
(`test_openhands_adapter.py:226-259`) documents this as expected: a stuck
worker keeps running until `max_iteration_per_run` ticks down.

**Severity**: P2 — known leak path during timeout, already tested. Workers run
sequentially in one container per run, so a single leaked thread does not
collide with a sibling worker. Across runs, threads live in different
processes.

**Fix**: None required for the per-run topology. If you ever cancel mid-run,
plumb the future cancellation into the underlying SDK.

---

### 6. Conversation/session sharing

**Finding**: A fresh `Conversation` is built per `_execute_task` call:

```python
# openhands_adapter.py:143-146
conversation = cast(
    "_ConversationLike",
    self._build_conversation(working_dir, mcp_servers),
)
```

`_build_conversation` (line 217-308) imports the SDK, constructs a new `LLM`,
new `Agent`, and new `Conversation` every time. No history, tool-call IDs, or
working directory carry across worker invocations on the shared adapter
instance.

**Severity**: P3.

**Fix**: None required.

---

### 7. Working directory race

**Finding**: `_execute_task` receives `working_dir` from `task_context`
(via `validate_task_context` at `infrastructure/adapters/worker/shared/validation.py:8-30`).
The OpenHands `Conversation` is created with `workspace=working_dir`
(`openhands_adapter.py:306`). Inside one run the workers execute sequentially,
each driven by the manager that knows its own working directory — they are not
forced into a shared `/workspace`. There is no cleanup-between-workers logic in
the adapter; cleanup (if any) is delegated to the container-management layer
(out of this scope).

**Severity**: P3 from the adapter's perspective. The actual workspace policy
lives in the security plugin / docker runtime (out-of-scope agent owns those).

**Fix**: None at the adapter layer.

---

### 8. TerminalTool on host (not container) — cross-run host fan-out

**Finding**: The adapter forces TerminalTool to use a subprocess backend that
runs on the HOST, not inside the container:

```python
# openhands_adapter.py:287-292
tools.append(
    Tool(
        name=TerminalTool.name,
        params={"terminal_type": "subprocess"},
    )
)
```

The comment at line 281-285 documents the choice: tmux PTY stalls on complex
nested quoting. The container-routing for build/test commands is implemented
via the `wrap_shell_command` helper (`shared/container_session.py:145-169`),
which prepends `docker exec -i ...` to commands the SDK runs through the host
subprocess. Inside one run, workers run sequentially so the host fork-count is
bounded by one. Across 10 parallel runs, the host carries 10 simultaneous SDK
subprocess shells plus their forked tool processes plus the 10 `docker exec`
descendants of each.

This matches `agent-docs/parallel-20-host-saturation-diagnosis.md` which has
already characterised the host saturation symptom. No collision risk on temp
files / named pipes / sockets was found in the adapter code; OpenHands' own
subprocess terminal manages its file descriptors per `Conversation`.

**Severity**: P1 (already documented; cross-reference, not re-derive).

**Fix**: See the linked diagnosis doc. From this audit's perspective, the
adapter is doing what the SDK exposes.

---

### 9. MCP server lifecycle

**Finding**: MCP server processes are launched by the SDK's `create_mcp_tools`
when the `Conversation` is constructed (`openhands_adapter.py:303` —
`Agent(llm=llm, tools=tools, mcp_config=mcp_config)`). The stdio spec passed in
(`shared/mcp_config.py:41-43`) is the raw `{command, args, env}` produced by
the security plugin. No port/PID is allocated by this adapter; the SDK manages
process lifecycle.

The adapter calls `pause()` then `close()` on the `Conversation` during cleanup
(`_request_shutdown` at `openhands_adapter.py:615-627`), so MCP processes are
torn down when the `Conversation` goes out of scope. Best-effort —
`close()`/`pause()` failures are swallowed at line 622-627. If `close()` is a
no-op, MCP server subprocesses leak until the run process exits; with 10 runs
all draining at end-of-run this can leave a brief fan-out of zombies but each
run process is short-lived enough to garbage-collect on exit.

**Severity**: P2 — leak window during abnormal termination, bounded by process
lifetime.

**Evidence**: `openhands_adapter.py:615-627`, `shared/mcp_config.py:41-43`.

**Fix**: If the SDK does not reliably reap MCP children on `close()`, add a
local registry of MCP PIDs and SIGTERM them in `_request_shutdown` before the
executor shutdown. Verify SDK behaviour first.

---

### 10. Sequencer / event emission from worker

**Finding**: `EventSequencer` is created PER `run_session` in the base class:

```python
# worker/base.py:109-111
def _create_sequencer(self, agent_id: UUID) -> EventSequencer:
    return EventSequencer(agent_id, stream=self.STREAM_NAME)
```

`EventSequencer.__init__` starts `_sequence = 2`
(`shared/event_sequencer.py:40-51`). It is instance-local; the adapter holds no
sequencer reference between calls. Two workers (sequential in one run) get two
separate sequencers, each starting from its own `start_sequence`. Sequence
collisions inside one aggregate would require two sequencers to share an
`agent_id` — that happens only if the orchestrator reuses an `agent_id` across
worker invocations (out of this audit's scope; the postgres event-store unique
constraint on `(aggregate_id, sequence_number)` would catch it).

**Severity**: P3.

**Fix**: None at the adapter layer.

---

## Claude Code adapter

### 11. ClaudeAgentSDKAdapter + ClaudeCodeWorker

**SDK adapter** (`infrastructure/adapters/worker/claude_sdk_adapter.py`):

- Per-call `asyncio.Queue` at line 96 — local to each `_execute_task`.
- `ClaudeSDKClient` opened as an `async with` at line 112 — torn down on exit.
- No shared SDK state held on the adapter instance beyond `self.config`
  (read-only after construction).

**CLI worker** (`infrastructure/workers/claude_code_worker.py`):

- Each `run_task` call creates a fresh `tempfile.TemporaryDirectory`
  (`claude_code_worker.py:156` host path, line 228 container path) for
  `CLAUDE_CONFIG_DIR`. Names are unique per call by virtue of `tempfile`
  randomization; no collision risk across workers in one run or across runs.
- `_exec` writes the transcript to `run_dir/stdout_stderr.log`
  (`claude_code_worker.py:92`). `run_dir = workspace.root` — assumed unique per
  worker by the orchestrator; if two workers in one run share a `workspace.root`
  they would overwrite. Cross-run isolation is fine because each OS process
  gets its own runs directory tree.
- `subprocess` lifetime: `await process.wait()` then drop. On timeout
  (`claude_code_worker.py:477-490`) the subprocess is killed and waited.

**Severity**: P3 — no shared mutable state. The transcript-overwrite condition
above is a contract requirement on the orchestrator, not an adapter bug.

**Fix**: None required at the adapter layer.

---

## Cross-run amplification

### 12. Shared API keys — retry storm with no jitter — **P0**

**Finding**: `LiteLLMAdapter._call_litellm` (`litellm_adapter.py:109-184`)
retries 429 / 503 with deterministic exponential backoff and NO jitter:

```python
# litellm_adapter.py:105-107
_LLM_TIMEOUT_SECONDS = 180
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BASE_DELAY = 10
...
# litellm_adapter.py:142-151
except (litellm.exceptions.RateLimitError, litellm.exceptions.ServiceUnavailableError) as e:
    last_error = e
    if attempt < self._RATE_LIMIT_RETRIES:
        delay = self._RATE_LIMIT_BASE_DELAY * (2 ** attempt)
        logger.warning(
            "Rate limited by %s (attempt %d/%d), retrying in %ds",
            model, attempt + 1, self._RATE_LIMIT_RETRIES + 1, delay,
        )
        await asyncio.sleep(delay)
        continue
```

Delays are exactly 10s, 20s, 40s. There is **no `random.uniform` / jitter**, no
cross-process lock, no shared rate-limit pool. With 10 OS processes sharing one
Anthropic/OpenAI key:

- All 10 hit 429 at roughly the same time.
- All 10 sleep exactly 10s → wake simultaneously → 10 simultaneous retries → 10
  more 429s.
- All 10 sleep exactly 20s → ... → 10 more 429s.
- After 70s of wall-clock and 4 attempts, all 10 raise `LLMError`
  (`litellm_adapter.py:152-155`).

This is a classic thundering herd. Anthropic's 529 (`overloaded_error`) IS
mapped by LiteLLM to `ServiceUnavailableError` (verified at
`.venv/lib/python3.12/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py:655,
727, 746`), so the retry path DOES cover it — but synchronously across all 10
processes.

`litellm.exceptions.APIError` is NOT retried (`litellm_adapter.py:163-167`
raises immediately). Any 500 / unknown 5xx from the provider becomes a hard
fail on the first attempt with zero retries.

**Severity**: P0 for the stated 10-parallel topology.

**Fix (minimal)**:

```python
import random
delay = self._RATE_LIMIT_BASE_DELAY * (2 ** attempt)
delay *= random.uniform(0.5, 1.5)   # full jitter window
await asyncio.sleep(delay)
```

A more principled fix is a per-key token bucket shared via Redis / file lock,
but the one-line jitter alone breaks the synchronised wake-up.

---

### 13. Global model registries

**Finding**: Two model registries are loaded into memory:

1. `infrastructure/adapters/worker/shared/cost_calculator.py:33-49` —
   `MODEL_PRICING: dict[str, ModelPricing]` is a module-level frozen-dataclass
   table. Read-only.
2. `infrastructure/adapters/cost_calculator.py:110-125` —
   `@lru_cache(maxsize=1)` over `_get_cached_model_cost_map()` which calls
   `litellm.get_model_cost_map(url="")`. `lru_cache` is thread-safe in CPython
   (the wrapper holds a per-cache `RLock`). LiteLLM's `get_model_cost_map(url="")`
   reads the bundled JSON shipped in the wheel — no network fetch, no
   `~/.cache/litellm/...` file write.

Across 10 OS processes, each process has its own `lru_cache` copy; first-call
latency is local to each process. No torn-write on a shared cache file because
no file-backed cache is in play.

**Severity**: P3.

**Fix**: None required.

---

## Other adapters in scope

### `infrastructure/adapters/recon_tool_adapter.py`

- Holds `self._workdir` (mutable via `set_working_directory`) but read-only
  during query execution.
- Each query uses `await asyncio.create_subprocess_exec` for grep/ctags
  (lines 197-208, 339-345) with a 10s `wait_for` timeout. Per-call subprocess —
  no shared state between concurrent grep calls.
- **Severity**: P3.

### `infrastructure/adapters/sinks.py`

- File / stdout / string / callback sinks. No async; each `write` is
  synchronous. The `FileSink` opens, writes, closes per call (lines 41-47),
  meaning two concurrent writes to the same path can interleave at the OS
  level. Tests use this for testing only.
- **Severity**: P2 if any production code shares one `FileSink` for concurrent
  writes (would need a separate audit), P3 otherwise.

### `infrastructure/snapshot.py`

- One-shot atomic temp-file replace (`snapshot.py:52-57`). Unique tempfile per
  call. Not LLM/worker related; included for completeness. **Severity**: P3.

### `infrastructure/adapters/shared_context_adapter.py`

- Delegates to `PostgresEventStore`. Out of this scope (event store is another
  agent's slice). Not LLM/worker related.

---

## Summary table — shared vs. not-shared adapter state in one run

| State                                       | Shared across managers in one run? | Mutable after init? | Risk in one run? |
|---------------------------------------------|------------------------------------|---------------------|------------------|
| `litellm` module globals                    | SHARED (library-internal)          | NO (adapter never mutates) | none           |
| `LiteLLMAdapter` instance                   | SHARED (one per run, bootstrap:100)| `default_config` read-only after init | none      |
| `LiteLLMAdapter._call_litellm` retry state  | per-call (`last_error` local)      | per-call            | none           |
| `litellm.acompletion` call kwargs           | per-call                           | per-call            | none           |
| `LLMFormatRepairer` instance + LLM port     | SHARED (same `LiteLLMAdapter`)     | read-only after init| amplification (#4) |
| `OpenHandsAdapter` instance                 | SHARED (one per run, composition:136) | config-only      | none           |
| `Conversation`, `LLM`, `Agent`              | per-task (`_build_conversation`)   | n/a                 | none           |
| `ThreadPoolExecutor`                        | per-task                           | n/a                 | leak on timeout (#5) |
| `EventSequencer`                            | per `run_session`                  | local counter       | none           |
| Workspace / `working_directory`             | per-task                           | n/a                 | none at adapter|
| MCP server subprocess (per `Conversation`)  | per-task                           | SDK-managed         | leak on close failure (#9) |

## Summary table — shared vs. not-shared resources across the 10 runs

| Resource                                  | Shared across 10 runs? | Audit issue here? |
|-------------------------------------------|------------------------|-------------------|
| Anthropic / OpenAI API key (env var)      | SHARED (single key)    | YES — retry storm (#12) |
| `litellm` library code & bundled cost JSON| each process has its own copy | none           |
| `lru_cache` model cost map                | per-process            | none           |
| Host shell processes (OpenHands subprocess terminal) | host-wide          | YES — known host saturation (#8, diagnosis doc) |
| docker daemon socket                      | host-wide              | out of scope    |
| Postgres                                  | host-wide              | out of scope (event-store agent) |
| Filesystem (`tempfile.TemporaryDirectory`)| unique names per call  | none           |

---

## Findings ranked

| #   | Severity | Title                                                    | File:line                                                       |
|-----|----------|----------------------------------------------------------|-----------------------------------------------------------------|
| 12  | P0       | 429/503 retry has no jitter → synchronised wake-up across 10 processes | `infrastructure/adapters/litellm_adapter.py:105-151`            |
| 4   | P1       | Format repairer routes through SAME shared `LiteLLMAdapter` → call-volume burst on parse-fail | `bootstrap/infrastructure.py:100-112`; `infrastructure/adapters/llm_format_repairer.py:84-103` |
| 8   | P1       | TerminalTool runs on host; 10 parallel runs → host saturation | `infrastructure/adapters/worker/openhands_adapter.py:287-292` (already documented in `agent-docs/parallel-20-host-saturation-diagnosis.md`) |
| 9   | P2       | MCP server child processes torn down by `Conversation.close()`; swallowed failures leak processes | `infrastructure/adapters/worker/openhands_adapter.py:615-627`   |
| 5   | P2       | `ThreadPoolExecutor` shutdown with `wait=False` on timeout; SDK thread can outlive grace period | `infrastructure/adapters/worker/openhands_adapter.py:157-215`   |
| —   | P2       | `JSONDecodeError` from `json.loads(args)` on tool-call arguments NOT wrapped in `_call_litellm` retry/catch | `infrastructure/adapters/litellm_adapter.py:329-331`            |
| —   | P2       | `_request_shutdown` calls SDK `pause()`/`close()` synchronously in `finally`; if SDK blocks, the event loop blocks | `infrastructure/adapters/worker/openhands_adapter.py:213-215, 615-627` |
| 1   | P3       | No code in this codebase mutates `litellm` module globals (informational) | `infrastructure/adapters/litellm_adapter.py:15` and grep        |
| 2   | P3       | `DefaultCostCalculator` is pure + thread-safe (informational)| `infrastructure/adapters/cost_calculator.py:110-125, 199-216`   |
| 3   | N/A      | No streaming code path                                   | `infrastructure/adapters/litellm_adapter.py` (entire file)      |
| 6   | P3       | `Conversation` is per-task                               | `infrastructure/adapters/worker/openhands_adapter.py:143-146, 217-308` |
| 7   | P3       | `working_dir` per-task; no shared workspace at adapter   | `infrastructure/adapters/worker/openhands_adapter.py:306`       |
| 10  | P3       | `EventSequencer` per `run_session`                       | `infrastructure/adapters/worker/base.py:109-111`; `shared/event_sequencer.py:40-51` |
| 11  | P3       | Claude Code: SDK + CLI both per-call; tempdir randomised | `infrastructure/adapters/worker/claude_sdk_adapter.py:96, 112`; `infrastructure/workers/claude_code_worker.py:156, 228` |
| 13  | P3       | Model registries: in-memory only, `lru_cache` thread-safe, no shared cache file | `infrastructure/adapters/cost_calculator.py:110-125`; `infrastructure/adapters/worker/shared/cost_calculator.py:33-49` |
