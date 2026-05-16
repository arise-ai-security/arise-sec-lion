# Experiment Architecture (A / B / C cells)

Reverse-engineered from the codebase via Serena symbol lookups. Every claim
below is annotated with `file:line` references to the actual implementation,
not docs. The system is hexagonal (`core/` ↔ `core/ports/*` ↔
`infrastructure/`), event-sourced on a single aggregate (`AgentSession`),
with a single composition root (`bootstrap/composition.py`).

The document has two layers:

- **§0 Actor-level overview** — one high-level interaction diagram per
  cell. Read these first.
- **§1–§10 Mechanism details** — concrete code paths, argv shapes, mount
  layouts, prompt assembly, event flow. Read these when you need to map
  an actor to actual code.

---

## 0. Actor-level overview (per cell)

These are deliberately abstract: each box is one *actor* (a process,
external service, or shared filesystem), not a class. Arrows are
labeled with the protocol on the wire so you can tell stdio from HTTP
from bind-mount-shared-state at a glance. The legend below applies to
all three diagrams.

```
   Legend
   ──────
   ─── HTTPS ──────────►  cross-network call to a remote API
   ─── stdio ──────────►  local subprocess pipe (FastMCP servers, claude CLI). NOT used for
                           the OpenHands SDK worker, which is in-process on a thread.
   ─── in-process ─────►  same-process call (e.g. orchestrator → Claude SDK / OpenHands SDK)
   ─── docker exec ────►  command sent into a long-lived container
   ─── bind mount ─────►  shared filesystem between host and container (no IPC)
   ─── tcp (Postgres) ─►  event-store write (host:port configurable via POSTGRES_HOST /
                           POSTGRES_PORT; local default 5432, dev overlay uses Supabase 6543)
   ─── argv ───────────►  arg-list passed at process spawn (e.g. rendered prompt as "--" tail)
   ─── SSE / queue ────►  realtime event broadcast to subscribers (query API)
```

### Cell A — flat, single in-container Claude CLI

The coding actor lives **inside** the CVE container; the host process is
just an orchestrator + event recorder. There is no Boss/Manager actor.

```
   ┌─────────────┐
   │ Researcher  │
   └──────┬──────┘
          │ run-matrix.py
          ▼
   ┌─────────────────────────┐
   │ Harness                  │  (experiments/shared/harness.py)
   │ (one process per study)  │
   └──────┬──────────────────┘
          │ subprocess per (cell, task, replicate)
          │ argv = `python main.py -c <cfg> run <task> --domain security …`
          ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ main.py runner (HOST)                                              │
   │  • SecurityDomainPlugin   ── prepares src/ + testcase/ on HOST     │
   │  • DockerSecBenchRuntime  ── starts CVE container                  │
   │  • ClaudeCodeWorker       ── builds CLI argv + docker exec wrapper │
   │  • PostgresEventStore     ── writes every domain event             │
   │  • RealtimeCallback       ── re-broadcasts events to query API     │
   └─────┬──────────────────┬──────────────┬──────────────────┬─────────┘
         │ docker run /     │ tcp          │ SSE / queue       │ events.jsonl
         │ docker exec      │ (Postgres)   │ (EventBroadcaster │ written
         │ (prompt in       ▼              │  → /events SSE)   │ post-run
         │  argv tail)  ┌──────────┐       ▼                   │ by harness
         │              │ Postgres │  ┌──────────────────┐     │
         │              └──────────┘  │ query API (FastAPI│     │
         │                            │  + React SPA)    │     │
         │                            └──────────────────┘     │
         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ CVE Container  (image: secb-tools:<tag>)                            │
   │                                                                    │
   │   ┌──────────────────────┐         ┌────────────────────────────┐  │
   │   │ claude CLI            │ stdio   │ MCP server (in-container)  │  │
   │   │ (the coding actor)    │◄───────►│ shell_in_container,        │  │
   │   │                       │         │ valgrind_run, klee_run     │  │
   │   │  Read/Write/Edit/Bash │         │  → bash -lc <cmd>          │  │
   │   │  on /src + /testcase  │         │    (no docker exec hop)    │  │
   │   └─────┬─────────────────┘         └────────────────────────────┘  │
   │         │ HTTPS                                                     │
   └─────────┼────────────────────────────────────────────────────────────┘
             │
             ▼
        ┌─────────────────────────┐
        │ Anthropic API           │
        │ (api.anthropic.com)     │
        └─────────────────────────┘

   Filesystem actor (shared):
       runs/<root_id>/  ──bind mount──►  /arise-run inside container
           ├── src/      ── /src
           └── testcase/ ── /testcase
```

Key intuition for A: only one LLM-driven loop exists, and its filesystem
view is the container's. A1 vs A2 toggles whether that loop is allowed to
spawn its own native subagents via the `Task` tool.

### Cell B — hierarchical, Claude Agent SDK worker on host

Now there are three *kinds* of LLM-driven actors: BOSS, MANAGER, WORKER —
all on the host. WORKER edits files on the host bind-mount and shells
into the container; BOSS/MANAGER never touch Docker.

```
   ┌─────────────┐
   │ Researcher  │
   └──────┬──────┘
          │
          ▼
   ┌─────────────────────────┐
   │ Harness                  │
   └──────┬──────────────────┘
          │ subprocess
          ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ main.py runner (HOST)                                                │
   │                                                                      │
   │   ┌──────────────────────────────────────────────────────────┐       │
   │   │  AgentExecutionService.run_system_loop                    │       │
   │   │  ┌────────┐  spawns  ┌──────────┐  spawns  ┌────────┐    │       │
   │   │  │ BOSS    │────────►│ MANAGER  │────────►│ WORKER  │    │       │
   │   │  │ (LLM)   │         │ (LLM)    │         │ (LLM)   │    │       │
   │   │  └───┬────┘         └────┬─────┘         └───┬────┘     │       │
   │   │      │ decompose         │ decompose         │ execute  │       │
   │   │      │ (may invoke       │ (may invoke       │          │       │
   │   │      │  recon tools      │  recon tools      │          │       │
   │   │      │  via LLM tool-    │  via LLM tool-    │          │       │
   │   │      │  calling on host) │  calling on host) │          │       │
   │   │      ▼                   ▼                   ▼          │       │
   │   │   LLMPort (OpenRouter or LiteLLM)        WorkerToolPort │       │
   │   │      │                                       │          │       │
   │   │      │ HTTPS                                 │ in-      │       │
   │   │      ▼                                       │ process  │       │
   │   │  ┌──────────────────┐                        ▼          │       │
   │   │  │ Anthropic API    │               ┌──────────────────┐│       │
   │   │  │ (BOSS+MANAGER    │               │ ClaudeAgentSDK   ││       │
   │   │  │  reasoning,      │               │ adapter (host    ││       │
   │   │  │  & Judge if      │               │ async loop)      ││       │
   │   │  │  skip_judge=false│               └──────┬───────────┘│       │
   │   │  └──────────────────┘                      │ HTTPS      │       │
   │   │                                            ▼            │       │
   │   │                                  ┌──────────────────┐  │       │
   │   │                                  │ Anthropic API    │  │       │
   │   │                                  │ (worker turn)    │  │       │
   │   │                                  └──────────────────┘  │       │
   │   │                                                          │       │
   │   │   WORKER prompt is enriched at dispatch time with         │       │
   │   │   SiblingViewPort.build_view(...) — a digest of           │       │
   │   │   completed-sibling artefacts (handoff context)           │       │
   │   │   (role_dispatch.py:99, prompt_builder.py:469)            │       │
   │   └──────────────────────────────────────────────────────────┘       │
   │                                                                      │
   │       │ Read/Write/Edit on cwd=host_root                              │
   │       │ Bash → can_use_tool rewrites to `docker exec -i <cid> …`      │
   │       │                                                               │
   │       │ stdio                                                         │
   │       ▼                                                               │
   │   ┌──────────────────────────────────┐                               │
   │   │ MCP server (HOST mode)            │── secb-exec ──► docker exec  │
   │   │  shell_in_container/valgrind/klee │                              │
   │   └──────────────────────────────────┘                              │
   └──────────────────────────────────────────────────────────────────────┘
             │ docker exec                                                
             │ + bind mount                                               
             ▼                                                            
   ┌────────────────────────────────────────────────────────┐
   │ CVE Container (one per WORKER agent_id)                │
   │   /src, /testcase, /arise-run  — visible to host       │
   │   No LLM inside; only build/test/valgrind commands run │
   └────────────────────────────────────────────────────────┘

   Postgres (event store) sits alongside, receiving events from every actor
   transition in the system loop. Skip-judge toggles whether the LLM Judge
   stage is invoked after each WORKER completes.
```

Key intuition for B: the *thinking* (decompose, plan, write code, verify)
happens on the host with three different prompts at three roles, but the
*hands* (filesystem + build + test) reach into the container. The same
Claude model serves all four roles; only the prompt template differs.

### Cell C — hierarchical, OpenHands worker on host

Same Boss/Manager skeleton as B; the WORKER is replaced by an OpenHands
agent loop that runs in a thread, talks to a different LLM (often
Qwen via Ollama Cloud), and has *no* built-in shell — every shell
command must go through MCP.

```
   ┌─────────────┐
   │ Researcher  │
   └──────┬──────┘
          ▼
   ┌─────────────────────────┐
   │ Harness                  │
   └──────┬──────────────────┘
          │ subprocess
          ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ main.py runner (HOST)                                                │
   │                                                                      │
   │   ┌────────┐  spawns  ┌──────────┐  spawns  ┌──────────┐              │
   │   │ BOSS    │────────►│ MANAGER  │────────►│ WORKER    │  (BOSS +     │
   │   │ Claude  │         │ Claude   │         │ OpenHands │   MANAGER   │
   │   │  via    │         │  via     │         │ SDK loop  │   identical │
   │   │ LLMPort │         │ LLMPort  │         │ on thread │   to Cell B,│
   │   │ (may    │         │ (may    │         │ (in-      │   incl.     │
   │   │  call   │         │  call   │         │ process   │   sibling   │
   │   │  recon) │         │  recon) │         │ via       │   handoff)  │
   │   │         │         │         │         │ Worker-   │             │
   │   │         │         │         │         │ ToolPort) │             │
   │   └───┬────┘         └────┬─────┘         └───┬──────┘              │
   │       │ HTTPS              │ HTTPS             │ HTTPS via           │
   │       ▼                    ▼                   │ OpenHands'          │
   │   ┌──────────────────────────────┐             │ embedded LiteLLM    │
   │   │ Anthropic API                │             ▼                     │
   │   │ (Boss+Manager+Judge if on)   │     ┌──────────────────────────┐  │
   │   └──────────────────────────────┘     │ Ollama Cloud  /  OpenAI  │  │
   │                                         │  /  Anthropic            │  │
   │                                         │ (worker model — Qwen by  │  │
   │                                         │  default in headline C1) │  │
   │                                         └──────────────────────────┘  │
   │                                                                      │
   │   WORKER's hands:                                                    │
   │     • FileEditorTool  ── operates on cwd=host_root (bind-mounted)    │
   │     • NO TerminalTool registered  ← intentional                       │
   │     • Shell commands go ONLY through MCP shell_in_container          │
   │                                                                      │
   │     │ stdio                                                          │
   │     ▼                                                                │
   │   ┌──────────────────────────────────┐                              │
   │   │ MCP server (HOST mode)            │── secb-exec ──► docker exec │
   │   │  shell/valgrind/klee              │                             │
   │   └──────────────────────────────────┘                             │
   └──────────────────────────────────────────────────────────────────────┘
             │ docker exec + bind mount
             ▼
   ┌────────────────────────────────────────────────────────┐
   │ CVE Container (one per WORKER agent_id)                │
   │   /src, /testcase, /arise-run                          │
   │   Receives every build / test / valgrind / klee call   │
   └────────────────────────────────────────────────────────┘
```

Key intuition for C: only the *worker actor changes* between B and C —
its LLM, its toolbox, and the fact that it can't shell directly. The
orchestration plus filesystem boundary are identical.

### Shared side-channels (all three cells)

```
   Every domain event emitted by any actor inside main.py is fanned out twice:

      AgentSession event ─► PostgresEventStore         (durable, tcp Postgres)
                         ─► RealtimeCallback           (in-process)
                                ▼
                         EventBroadcaster (in-process queue)
                                ▼
                         query/api/routes/events.py    (FastAPI SSE)
                                ▼
                         dashboard SPA (`query/web/`)

   The harness does NOT consume this SSE stream — it only reads the per-run
   manifest + projection — but the React dashboard does, which is why each
   actor in the diagrams above is also implicitly an event producer.
```

A note on BOSS/MANAGER tool access: the diagrams show BOSS/MANAGER reaching
only the LLM, but they can additionally invoke **host-side recon tools**
(`read_file`, `list_directory`, `search_codebase`, etc.) when the LLM emits
a tool call and the cell's `recon.allowed_tools` allows it
(`core/application/agent_orchestrator.py:447`, `config/config.yaml:97`).
These calls never reach Docker — they read the host mirror under
`host_root/src` directly. Some configs disable this; check the active
config's `boss.recon.allowed_tools` / `manager.recon.allowed_tools` block.

### Cross-cell actor summary

| Actor                             | Cell A             | Cell B                          | Cell C                          |
| --------------------------------- | ------------------ | ------------------------------- | ------------------------------- |
| Initiator (human)                 | Researcher         | Researcher                      | Researcher                      |
| Orchestration process             | `main.py` runner   | `main.py` runner                | `main.py` runner                |
| Decomposer (LLM)                  | —                  | BOSS + MANAGER (Claude)         | BOSS + MANAGER (Claude)         |
| Sibling-context input to WORKER   | n/a                | SiblingViewPort handoff         | SiblingViewPort handoff         |
| Coder (LLM)                       | Claude (CLI)       | Claude (SDK, async in-process)  | Qwen / OpenAI / Claude on thread|
| Coder runtime                     | CVE container      | Host                            | Host                            |
| Coder filesystem view             | container `/src`   | host bind-mount                 | host bind-mount                 |
| Shell command path                | in-container       | SDK Bash → `docker exec` OR MCP | MCP only                        |
| Verifier (LLM Judge)              | n/a                | optional (`skip_judge`)         | optional (`skip_judge`)         |
| Event store (durable)             | Postgres           | Postgres                        | Postgres                        |
| Event side-channel                | EventBroadcaster→SSE | EventBroadcaster→SSE          | EventBroadcaster→SSE            |
| Per-WORKER container              | 1 (the only one)   | 1                               | 1                               |

---

## 1. Cell taxonomy (what changes between A, B, C)

Cells share the same host process, LLM gateway, MCP tool server, and CVE
container plumbing. Differences span `orchestration.mode`, `worker.tool`,
the worker model, the BOSS/MANAGER model, allowed/disallowed tools,
`skip_judge`, and timeouts.

| Cell    | `orchestration.mode` | Coder (`worker.tool`) | Coder model / runtime                                       | BOSS / MANAGER LLM   | `skip_judge` | Notable tool diff |
| ------- | -------------------- | --------------------- | ------------------------------------------------------------ | -------------------- | ------------ | ----------------- |
| **A1**  | `flat`               | `claude_code` (CLI)   | `claude-sonnet-4-6`, CLI runs INSIDE the CVE container        | — (no Boss/Manager)  | n/a          | `Task` allowed (Claude native subagent) |
| **A2**  | `flat`               | `claude_code` (CLI)   | `claude-sonnet-4-6`, CLI runs INSIDE the CVE container        | — (no Boss/Manager)  | n/a          | `Task` in `disallowed_tools` (single agent) |
| **B1**  | `hierarchical`       | `claude_code` (SDK)   | `claude-sonnet-4-6`, SDK on host → Anthropic API              | `claude-sonnet-4-6`  | `true`       | allowed Read/Write/Edit/MultiEdit/Bash/Glob/Grep, deny WebSearch/WebFetch |
| **B2**  | `hierarchical`       | `claude_code` (SDK)   | `claude-sonnet-4-6`, SDK on host → Anthropic API              | `claude-sonnet-4-6`  | `false`      | `allowed_tools: ["*"]`, `disallowed_tools: []` |
| **C1**  | `hierarchical`       | `openhands` (SDK)     | `ollama_chat/qwen3.5:397b-cloud`, OpenHands SDK on host       | `claude-sonnet-4-6`  | `true`       | timeout 5400s, 40 iters |
| **C2**  | `hierarchical`       | `openhands` (SDK)     | `ollama_chat/qwen3.5:397b-cloud`, OpenHands SDK on host       | `claude-sonnet-4-6`  | `false`      | judge ON                |

Sources: `experiments/2026-05-11-fresh-start/configs/{A1,A2,B1,B2,C1,C2}*.yaml`,
`experiments/2026-05-11-fresh-start/manifest.yaml:17` (`headline_cells`).
Headline C1 is **Qwen via Ollama Cloud** (`C1-qwen-noverifier.yaml`). The
`C1-openhands-openai-smoke.yaml` and `C1-openhands-claude-smoke.yaml` files
are smoke variants (`C1smoke`, `C1claudesmoke`) that pin OpenAI / Anthropic
to isolate the worker-runtime stack; they are NOT the headline-C1 model.

A1 vs A2: A1 allows the `Task` subagent tool (Claude may decompose into its
own children using its native subagent mechanism); A2 disallows it,
collapsing the run to a single Claude agent. Cells B/C differ in the
**WORKER** model and runtime; BOSS+MANAGER reasoning is always
`claude-sonnet-4-6` on the host (`config/config.yaml` planner default
overridden only in some smoke variants).

---

## 2. Process / host topology (laptop side)

```
┌─────────────────────────────── HOST MACHINE (Mac laptop, darwin) ───────────────────────────────┐
│                                                                                                  │
│  ╭─ Experiment harness (per (cell, task, replicate)) ─────────────────────────────────────────╮  │
│  │ experiments/shared/harness.py:run_arise()                                                   │  │
│  │   └─ _invoke_main_py()  →  subprocess: `python main.py -c <cfg.yaml> run <task>            │  │
│  │                                            --domain security                               │  │
│  │                                            --domain-context-file <ctx.json>`               │  │
│  │  Sets ARISE_RUN_RESULT_PATH=<tmp> so the child writes back {root_id, status}.              │  │
│  ╰────────────────────────────────────────────────────────────────────────────────────────────╯  │
│                                                                                                  │
│  ╭─ Child process: `python main.py … run …` (one per CVE replicate) ──────────────────────────╮  │
│  │  main.py:11  →  bootstrap.bootstrap.main()                                                 │  │
│  │  bootstrap/bootstrap.py:168 _run_task()                                                    │  │
│  │    1. Load Settings (config/config.yaml ← config.<env>.yaml ← env vars).                   │  │
│  │    2. get_run_domain_components() → SecurityDomainPlugin                                   │  │
│  │       (plugins/security/plugin.py:44).                                                     │  │
│  │    3. plugin.infer_context(task_text, ctx_file) → CVEInstance.                             │  │
│  │    4. create_runtime_cli() (bootstrap/composition.py:264) builds Infrastructure +          │  │
│  │       Application.                                                                         │  │
│  │    5. cli.run_task(task, domain_context=CVEInstance) (one big async).                      │  │
│  │    6. Write run_manifest.json + effective_config.yaml + (per-run summary projection if      │  │
│  │       Postgres responds).                                                                  │  │
│  │       (`events.jsonl` is projected by the HARNESS after the subprocess returns —           │  │
│  │        `experiments/shared/harness.py:327 project_events_to_jsonl()` — not by main.py.)    │  │
│  │                                                                                            │  │
│  │  Composition root only — sole place that imports `plugins/`                                │  │
│  │  (bootstrap/composition.py: `_build_security_components`).                                 │  │
│  ╰────────────────────────────────────────────────────────────────────────────────────────────╯  │
│                                                                                                  │
│  ╭─ Infrastructure adapters (singletons, wired by bootstrap/infrastructure.py:126) ─────────────╮  │
│  │  PostgresEventStore   ──► localhost:5432 (docker compose --profile local)                  │  │
│  │  LLMPort = OpenRouterAdapter OR LiteLLMAdapter   (switched by ARISE_LLM_GATEWAY env)       │  │
│  │      bootstrap/infrastructure.py:32 _build_llm_adapter                                     │  │
│  │  WorkerToolPort = ClaudeAgentSDKAdapter / OpenHandsAdapter / GoogleADKAdapter              │  │
│  │      bootstrap/infrastructure.py:91 _create_worker_adapter                                 │  │
│  │  SharedContextPort = PostgresSharedContextAdapter (read-side projections of events)        │  │
│  │  ReconToolPort = ReconToolAdapter (HOST-local read_file/list_dir/grep)                     │  │
│  │  FormatRepairerPort = LLMFormatRepairer (optional, repairs malformed JSON tool output)     │  │
│  ╰────────────────────────────────────────────────────────────────────────────────────────────╯  │
│                                                                                                  │
│  ╭─ Application layer (core/) ──────────────────────────────────────────────────────────────────╮  │
│  │  AgentExecutionService (core/application/execution_service.py:130)                         │  │
│  │   ├─ create_boss_agent → uuid4 root_id, materialize run dir (setup_working_directory),     │  │
│  │   │  then `plugin.prepare_run` → DockerSecBenchRuntime.prepare_workspace.                  │  │
│  │   ├─ run_system_loop(root_id)                                                              │  │
│  │   │    if mode=='flat': _run_flat_mode(root_id)   ← Cell A path                            │  │
│  │   │    else:             poll active agents + dispatch via build_role_handlers (B/C)       │  │
│  │   │                                                                                        │  │
│  │  AgentOrchestrator (core/application/agent_orchestrator.py:204)                            │  │
│  │   ├─ assess_task     ← PendingHandler (initial complexity check via LLM)                  │  │
│  │   ├─ evaluate_task   ← EvaluatorHandler (BOSS+MANAGER: ask LLM to decompose into subtasks) │  │
│  │   └─ execute_task    ← WorkerHandler   (WORKER: call worker_port.run_session(task_ctx))    │  │
│  │                                                                                            │  │
│  │  VerificationPipeline (core/application/services/orchestration/verification_pipeline.py)   │  │
│  │   ├─ structural / deterministic / execution stages (always on, host-side)                 │  │
│  │   └─ judge stage (skip_judge gates this; B2/C2 keep it, B1/C1 skip it)                    │  │
│  │      Judge uses LLMPort → OpenRouter/LiteLLM.                                              │  │
│  ╰────────────────────────────────────────────────────────────────────────────────────────────╯  │
│                                                                                                  │
│  ╭─ Per-run filesystem (HOST) ────────────────────────────────────────────────────────────────╮  │
│  │  runs/<root_id>/                                                                           │  │
│  │    ├─ events.jsonl              ← written by HARNESS post-subprocess (project_events_to_jsonl) │
│  │    ├─ run_manifest.json         ← written by main.py at end of _run_task                   │  │
│  │    ├─ effective_config.yaml     ← snapshot of resolved Settings                            │  │
│  │    ├─ src/        ⇆  bind-mounted into container at /src                                   │  │
│  │    ├─ testcase/   ⇆  bind-mounted into container at /testcase                              │  │
│  │    ├─ secb-exec   ← shell helper that wraps `docker exec` for the MCP server (host mode)   │  │
│  │    ├─ mcp_servers.in_container.json  ← rewritten MCP config (Cell A in-container path)     │  │
│  │    └─ stdout_stderr.log         ← Cell A: captured `claude` CLI transcript                 │  │
│  │                                                                                            │  │
│  │  Whole `runs/<root_id>/` is itself bind-mounted INTO the container at                      │  │
│  │  `<container_workspace_root>` = `/arise-run`                                               │  │
│  │  (container_runtime.py:31), so the helper script and the in-container MCP                  │  │
│  │  config file are reachable inside the container too.                                       │  │
│  ╰────────────────────────────────────────────────────────────────────────────────────────────╯  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

References: `bootstrap/bootstrap.py:168`, `bootstrap/composition.py:264`,
`bootstrap/infrastructure.py:32,91,126`,
`core/application/execution_service.py:227,331,420`,
`core/application/agent_orchestrator.py:240,434,499`,
`core/application/services/lifecycle/role_dispatch.py:178`,
`plugins/security/docker_runtime.py:67,105,322`,
`experiments/shared/harness.py:225,273`.

---

## 3. CVE container — what's inside and what's mounted

Every WORKER (or, in Cell A, the whole flat run) gets a fresh container per
agent. Build & test always happen inside this container; the host never
has `/src` or `/testcase`.

```
                    HOST                                          CONTAINER (per agent_id)
   ┌──────────────────────────────────┐                ┌──────────────────────────────────────────┐
   │ runs/<root_id>/src/   ─────────────────[bind ro/rw]─►  /src                                  │
   │ runs/<root_id>/testcase/ ──────────────[bind ro/rw]─►  /testcase                             │
   │ runs/<root_id>/        ────────────────[bind rw]──►   /arise-run                             │
   │                                  │                │      (container_workspace_root literal,  │
   │                                  │                │       container_runtime.py:31; so        │
   │                                  │                │       /arise-run/secb-exec and           │
   │                                  │                │       /arise-run/mcp_servers.in_container│
   │                                  │                │       .json are reachable in-container)  │
   │ runs/<root_id>/secb-exec         │                │                                          │
   │  (executable helper wrapping     │                │   Image: secbench/<vuln>:<tag>           │
   │   `docker exec … bash -lc "…"`)  │                │     ↑ resolved by                        │
   │                                  │                │       plugins/security/image_resolver.py │
   │ Container created by:            │                │     ↑ optionally re-tagged to            │
   │  DockerSecBenchRuntime.start_    │                │       secb-tools:<tag> when             │
   │   session                        │                │       security_tools_enabled is set      │
   │  (plugins/security/              │                │       (image_resolver.py:11,41).        │
   │   docker_runtime.py:105)         │                │       That image bakes in python +       │
   │                                  │                │       valgrind + klee + /opt/arise-mcp.  │
   │                                  │                │   Base CVE image default:                │
   │                                  │                │     hwiwonlee/secb.eval.x86_64           │
   │                                  │                │     /<repo>.<cve>:patch                  │
   │                                  │                │     (cve_instance.py:49)                 │
   │                                  │                │                                          │
   │  `docker run -d --network … \    │                │   Started with: `tail -f /dev/null`     │
   │     --label arise.root_id=… \    │                │   (long-lived idle container; commands   │
   │     --label arise.agent_id=… \   │                │    arrive via `docker exec`)             │
   │     -v src:/src -v tc:/testcase \│                │                                          │
   │     -v host_root:/arise-run \    │                │   After start_session() runs:            │
   │     <image> tail -f /dev/null`   │                │     • if cve.secb_sh: _install_secb()   │
   │                                  │                │       (docker_runtime.py:177 — secb_sh   │
   │                                  │                │        is OPTIONAL per-CVE bootstrap)    │
   │                                  │                │     • git safe.directory configured      │
   │                                  │                │     • /src/build.sh chmod +x             │
   │                                  │                │                                          │
   │  Concurrency invariant            │                │                                          │
   │  (plugins/security/plugin.py:196):│                │                                          │
   │  only ONE active worker container │                │                                          │
   │  per root_id at any time; a       │                │                                          │
   │  second start_session for the     │                │                                          │
   │  same root raises. Hierarchical   │                │                                          │
   │  workers are dispatched           │                │                                          │
   │  sequentially                     │                │                                          │
   │  (execution_service.py:376).      │                │                                          │
   └──────────────────────────────────┘                └──────────────────────────────────────────┘

   Source-of-truth: plugins/security/docker_runtime.py
       prepare_workspace  (67)  — extracts `/src` and `/testcase` from the image into host run dir
       start_session      (105) — `docker run`, bind mounts, security label set
       _write_exec_helper (322) — writes runs/<root_id>/secb-exec, then `chmod +x`
                                   helper falls back through $WORKDIR → /src → / (line 346)
       _install_secb      (311) — sources secb-bench helper inside the container, only if
                                   cve.secb_sh is set (177)
```

---

## 4. MCP server (custom tools: shell / valgrind / klee)

There is exactly **one** custom-tool MCP server, exposing three tools to
the WORKER. It is launched as a stdio subprocess by the worker engine
(Claude SDK or OpenHands SDK).

```
   plugins/security/mcp/security_tools_server.py  (FastMCP, stdio)

   @mcp.tool shell_in_container (191)  ── arbitrary `bash -lc` in the container's work_dir
   @mcp.tool valgrind_run       (228)  ── `valgrind --tool=memcheck --leak-check=full …`
   @mcp.tool klee_run           (256)  ── lazy `apt-get install -y klee` then `klee <bc>`

   Two transport modes (chosen by env vars; build_stdio_config:47):

   ┌───────────────────────────────────────────────────────────────────────────────────┐
   │ HOST MODE  (Cell B + Cell C)                                                      │
   │   env injected by build_stdio_config (47):                                        │
   │     • ARISE_SECBENCH_CONTAINER_ID = <container_id>                                │
   │     • ARISE_SECBENCH_HELPER_SCRIPT = <abs path to runs/<root_id>/secb-exec>       │
   │     • ARISE_SECBENCH_WORK_DIR = <cve.work_dir> (only if not None)                 │
   │   server runs on HOST  →  shells out:   `secb-exec "<command>"`                   │
   │                                              │                                    │
   │                                              ▼                                    │
   │                                         `docker exec -i -w "$WORKDIR" <cid>       │
   │                                            bash -lc "umask 000; <command>"`       │
   │                                         (with $WORKDIR → /src → / fallbacks)      │
   │                                                                                   │
   │   _exec_in_container (106) chooses argv via helper_script presence (115-120).     │
   │   The MCP server itself is launched by the worker adapter, NOT the orchestrator:  │
   │     • ClaudeAgentSDKAdapter._build_options:194 sets kwargs["mcp_servers"] = …     │
   │     • OpenHandsAdapter._build_conversation:347 passes mcp_config=…                │
   │                                                                                   │
   │ IN-CONTAINER MODE  (Cell A — claude CLI is itself `docker exec`'d)                │
   │   ClaudeCodeWorker._maybe_write_in_container_mcp_config (252) rewrites the JSON:  │
   │     command = "/opt/arise-mcp/venv/bin/python"                                    │
   │     args    = ["-m", "plugins.security.mcp.security_tools_server"]                │
   │     env  -= "ARISE_SECBENCH_HELPER_SCRIPT"  (signals in-container mode)           │
   │     env  += "PYTHONPATH=/opt/arise-mcp"                                           │
   │   _exec_in_container then routes via `bash -lc <cmd>` directly in the local       │
   │   container shell (no host hop, no `docker exec`).                                │
   └───────────────────────────────────────────────────────────────────────────────────┘
```

The MCP config (host-mode) is constructed by `SecurityDomainPlugin.prepare_worker_execution`
(`plugins/security/plugin.py:166`) and lives in `WorkerExecutionContext.task_context["mcp_servers"]`,
which the adapter then transcribes into its engine-specific shape (`mcpServers` for CLI,
`mcp_servers` kwarg for SDK, `MCPConfig` for OpenHands).

`ReconToolAdapter` is a **different, host-side** tool surface usable during
hierarchical BOSS/MANAGER reasoning — it operates purely on host paths
(pointed at the run's host_root via `execution_service.py:960`) and is not
routed via MCP. Available methods: `read_file`, `list_directory`,
`search_codebase`, `find_file`, `get_file_structure`,
`get_symbols_overview`, `read_symbol`
(`infrastructure/adapters/recon_tool_adapter.py`). The *actual* recon
surface BOSS/MANAGER can call at runtime is the subset allow-listed by
`ToolsetPolicyResolver` against `config/config.yaml:99-128` — which
typically exposes `search_codebase`, `read_file`, `get_file_structure`,
`get_symbols_overview`, `read_symbol`
(`core/application/services/toolset/toolset_policy_resolver.py:58`).

---

## 5. AI providers and gateway selection

```
   BOSS / MANAGER / Judge (host, hierarchical mode)
      └─► AgentOrchestrator._query_llm (core/application/agent_orchestrator.py:848)
            └─► LLMQueryExecutor
                  └─► LLMPort:
                       ┌─────────────────────────────────────────────────────────┐
                       │ ARISE_LLM_GATEWAY=openrouter →  OpenRouterAdapter        │
                       │    (openai SDK pointed at https://openrouter.ai/api/v1) │
                       │                                                         │
                       │ ARISE_LLM_GATEWAY=litellm   →  LiteLLMAdapter (default) │
                       │    multi-provider routing: OpenAI / Anthropic / Ollama  │
                       └─────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                     external HTTPS to AI provider APIs

   WORKER (Cell A flat — claude CLI inside the container)
      └─► argv = ["claude", "-p", "--output-format", "stream-json",
                  "--include-partial-messages", "--max-turns", "<N>",
                  "--mcp-config", "<path>", …, "--", "<rendered prompt>"]
          (no SDK; CLI subprocess wrapped in `docker exec`)
              └─► ANTHROPIC API directly  (CLI talks to api.anthropic.com itself)

   WORKER (Cell B — Claude Agent SDK on host)
      └─► ClaudeAgentSDKAdapter._execute_task (infrastructure/adapters/worker/claude_sdk_adapter.py:83)
            └─► async with ClaudeSDKClient(options=ClaudeAgentOptions(...)) as client:
                    await client.query(task_description)        (claude_sdk_adapter.py:114)
                  └─► ANTHROPIC API directly

   WORKER (Cell C — OpenHands SDK on host)
      └─► OpenHandsAdapter._build_conversation (infrastructure/adapters/worker/openhands_adapter.py:269)
            └─► openhands.sdk.LLM(model=…, base_url=…)
                  └─► OpenHands SDK uses its own embedded LiteLLM to dispatch to
                      OpenAI / Anthropic / Ollama Cloud (qwen3.5:397b-cloud).
                      This LiteLLM is OpenHands-internal — NOT the same instance as
                      our `LLMPort = LiteLLMAdapter` used by BOSS/MANAGER/judge.

   "Qwen-local" in practice resolves to Ollama Cloud (`ollama_chat/qwen3.5:397b-cloud`,
   C1/C2 configs); native_tool_calling is disabled (XML mock instead) and `think=false`
   is passed via litellm_extra_body. See openhands_adapter.py:269 comment block.
```

---

## 6. Cell-A flat mode — control & data flow

```
   Host process                                  Container (per root_id)
   ─────────────                                 ─────────────────────────

   AgentExecutionService._run_flat_mode  (420)
     │
     ├─ plugin.prepare_worker_execution
     │     → DockerSecBenchRuntime.start_session ─────► docker run -d … secb-tools image
     │
     ├─ flat_invariant_builder → FlatModeBundle
     │     TaskPromptSpec.rendered_prompt is assembled by
     │     core/application/services/prompt/prompt_builder.py:405 (build_flat_prompt).
     │     IMPORTANT: in flat mode the role/operation/worker Jinja templates are
     │     intentionally NOT rendered (prompt_builder.py:436-445); the worker prompt
     │     is composed only from:
     │       • SecBenchPromptStrategy.extend_flat_prompt (prompt_strategy.py:177)
     │         which renders domains/secbench/cve.j2 + flat_pipeline.j2 with the
     │         CVEInstance,
     │       • a subagent note if `Task` is enabled (prompt_builder.py:28),
     │       • the user <task> body wrapped in <task>…</task>.
     │
     ├─ ClaudeCodeWorker.run_task   (infrastructure/workers/claude_code_worker.py:80)
     │     │
     │     ├─ container_session present → _run_in_container (174)
     │     │     • _maybe_write_in_container_mcp_config (252) writes
     │     │       runs/<root_id>/mcp_servers.in_container.json
     │     │     • _build_argv (300) builds the argv:
     │     │         claude -p
     │     │           --output-format stream-json
     │     │           --include-partial-messages
     │     │           --max-turns <N>
     │     │           --mcp-config <path inside container>
     │     │           [--allowedTools …]   (only emitted when allowed != ("*",), line 338)
     │     │           [--disallowedTools …]
     │     │           --   <rendered prompt>      ← prompt is the trailing positional, NOT after -p
     │     │
     │     │     • _wrap_with_docker_exec (404) wraps that argv with:
     │     │         docker exec -i --user 1000:1000
     │     │           -w <container_working_directory>
     │     │           -e KEY=value …
     │     │           <container_id>  <claude_argv…>                                ─────────────►   claude CLI inside container
     │     │                                                                                          │
     │     │                                                                                          ├─► Read / Write / Edit / Bash / Glob / Grep
     │     │                                                                                          │     (+ `Task` if not in disallowed_tools)
     │     │                                                                                          │     all run inside the container, on /src + /testcase
     │     │                                                                                          │
     │     │                                                                                          ├─► spawns local stdio MCP subprocess:
     │     │                                                                                          │     /opt/arise-mcp/venv/bin/python -m
     │     │                                                                                          │     plugins.security.mcp.security_tools_server
     │     │                                                                                          │     (in-container mode → `bash -lc …` direct)
     │     │                                                                                          │
     │     │                                                                                          └─► HTTPS to api.anthropic.com
     │     │
     │     └─ stdout (stream-json transcript) captured to runs/<root_id>/stdout_stderr.log
     │        and converted into AgentSession worker events (_events_from_transcript:503)
     │
     ├─ apply terminal WorkerResult → BOSS aggregate transitions to COMPLETED/FAILED
     └─ plugin.cleanup_worker_execution → docker rm -f <container_id>
```

**A1 vs A2**: A1 keeps `Task` in the allow-list so the in-container Claude
CLI may use its native `Task` subagent tool. The actual subagent process
topology is owned by the Claude CLI (not by this codebase), so we don't
claim a specific in-container mechanism — what *is* observable is that
`bootstrap/composition.py:173` sets `subagent_enabled = "Task" not in
settings.worker.disallowed_tools`, and `prompt_builder.py:28-30` injects a
subagent-encouragement note into the flat prompt when enabled. A2 disallows
`Task`, collapsing the run to a single agent. Neither cell ever creates
BOSS/MANAGER aggregates — `_run_flat_mode` treats the root BOSS aggregate
purely as a sink for synthetic worker events
(`execution_service.py:421,487`).

---

## 7. Cell-B and Cell-C — hierarchical control flow

Identical orchestration shape; the WORKER tool, worker model, allowed/
disallowed tool lists, iteration caps, timeouts, and `skip_judge` all vary
across B1/B2/C1/C2 (see Section 1 table and the config diff in
`experiments/2026-05-11-fresh-start/configs/`). Boss/Manager decomposition
reasoning is **always** done on the host with Claude
(`boss.model = manager.model = claude-sonnet-4-6` in B1/B2/C1/C2 configs).

```
   AgentExecutionService.run_system_loop  (331)
   │
   │  active_agents = ShardedQueryService.get_active_agent_ids(root_id, sequential_workers=True)
   │  for each new active agent: asyncio.create_task(_run_agent_step_safe(agent_id))
   │
   │  Role dispatch:  role_dispatch.build_role_handlers(178)
   │     PENDING  → PendingHandler   → orchestrator.assess_task   (LLM call: complexity check)
   │     BOSS     ┐
   │     MANAGER  ┴→ EvaluatorHandler → orchestrator.evaluate_task (LLM call: decompose into Subtasks)
   │                                       └─ _spawn_children (649): atomic reserve via
   │                                          ChildAgentFactory → spawns child agents (role=PENDING)
   │     WORKER   → WorkerHandler    → orchestrator.execute_task
   │
   ▼
   AgentOrchestrator.execute_task  (499)   — only fires for AgentRole.WORKER
     ├─ prompt = PromptBuilder.build_worker_prompt(
     │            task_description, agent_id, handoff,
     │            workspace_context, domain_context, briefing)
     │   (handoff = SiblingViewPort.build_view → sibling artefacts as context)
     │
     ├─ task_context = {agent_id, task_description=prompt, tool_name, config,
     │                  working_directory=<host_root>,
     │                  container_session=<from WorkerExecutionContext>,
     │                  mcp_servers={"security_tools": build_stdio_config(...)}}
     │
     └─ async for event in WorkerToolPort.run_session(task_context):
            agent.apply_worker_event(event)             # event sourcing
            realtime_callback.on_event(event, root_id)  # console streaming
```

### 7a. Cell B WORKER path — Claude Agent SDK on host

```
   ClaudeAgentSDKAdapter._execute_task  (infrastructure/adapters/worker/claude_sdk_adapter.py:83)
     │
     ├─ task_description = apply_task_prefix(rendered_prompt, container_session,
     │                                       working_directory, auto_shell=True)
     │     (claude_sdk_adapter.py:99 — prefixes explicit "you are inside container; use
     │      Bash for shell commands; host paths X map to container paths Y" instructions)
     │
     ├─ _build_options (148)   →  ClaudeAgentOptions(
     │                              model=claude-sonnet-4-6,
     │                              cwd=<host_root>,                ← edits land on bind-mounted dir
     │                              allowed_tools=[Read,Write,Edit,MultiEdit,Bash,Glob,Grep],
     │                              mcp_servers={"security_tools": {…}},
     │                              hooks={"PostToolUse": [capture_tool_use]},
     │                              can_use_tool=<wraps container_session.translate_tool_input
     │                                            so Bash input is rewritten before execution>)
     │
     ├─ async with ClaudeSDKClient(options=options) as client:
     │     await client.query(task_description)        (claude_sdk_adapter.py:114-116)
     │     async for message in client.receive_response(): … process_message
     │    └─► HTTPS to api.anthropic.com
     │
     ├─ Tool routing — TWO shell paths coexist:
     │     • SDK's built-in `Bash` tool — can_use_tool intercepts the input and rewrites
     │       it to `docker exec -i <container_id> bash -lc "<cmd>"` so it runs INSIDE
     │       the CVE container (infrastructure/adapters/worker/shared/container_session.py:138).
     │     • MCP's `shell_in_container` tool — separately available; the host-mode MCP
     │       server shells out via `secb-exec` → `docker exec`.
     │     • Read/Write/Edit/MultiEdit operate on host paths under cwd=<host_root>; the
     │       bind mount makes those changes visible at /src instantly.
     │
     └─ each tool/result message is forwarded into the AgentSession aggregate.
```

### 7b. Cell C WORKER path — OpenHands SDK on host

```
   OpenHandsAdapter._execute_task (infrastructure/adapters/worker/openhands_adapter.py:161)
     │
     ├─ task_description = apply_task_prefix(rendered_prompt, container_session,
     │                                       working_directory, auto_shell=False)
     │     (openhands_adapter.py:173 — same prefix mechanism as Cell B but `auto_shell=False`
     │      because OpenHands has no Bash tool; agent is forced toward MCP shell_in_container)
     │
     ├─ OpenHandsAdapter._build_conversation (openhands_adapter.py:269) builds:
     │     openhands.sdk.LLM(model=<C1: ollama_chat/qwen3.5:397b-cloud
     │                              / C1smoke: openai/gpt-4o-mini
     │                              / C1claudesmoke: claude-haiku-4-5
     │                              / C2: ollama_chat/qwen3.5:397b-cloud>,
     │                       base_url=…, max_output_tokens=65000 for ollama,
     │                       native_tool_calling=False for ollama,
     │                       litellm_extra_body={"think": False, "num_ctx": 65536},
     │                       top_p=0.95)
     │     ↑ The OpenHands SDK uses LiteLLM internally to dispatch to the upstream
     │       provider — this is OpenHands' own concern, not our LLMPort gateway.
     │
     ├─ tools = [FileEditorTool]   ← intentionally NO TerminalTool (the host shell can't
     │                               see /src or /testcase; comment at line 332).
     │                               All shell work routes through MCP shell_in_container.
     │
     ├─ Agent(llm, tools=[FileEditorTool], mcp_config={"security_tools": {…}})
     │     Conversation(agent, workspace=<host_root>, max_iteration_per_run=…)
     │
     ├─ conversation.send_message(task_description)        (openhands_adapter.py:214)
     │  executor.submit(conversation.run)                  (openhands_adapter.py:215)
     │     ← OpenHands' .run is synchronous, so it's submitted to a ThreadPoolExecutor
     │       (unlike Cell B's Claude SDK which is native async).
     │
     └─ FileEditorTool reads/writes host_root/src/* — bind mount makes it visible
        inside the container instantly. Multi-turn agent loop emits
        Action/Observation/Thought events → adapter classifies & wraps them as
        domain events (ActionTaken / ObservationCaptured / ThoughtCaptured) →
        AgentSession.apply_worker_event.
```

### 7c. Where the "AI does coding" actually happens

| Cell | LLM that writes code                                   | Where its filesystem tools run                           | Where its shell commands run                                                                              |
| ---- | ------------------------------------------------------ | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| A    | Claude (CLI, in-container)                             | INSIDE the container (`/src`, `/testcase`)                | INSIDE the container directly (CLI's own Bash) + in-container MCP server (`bash -lc`, no docker exec hop) |
| B    | Claude (Anthropic API, host)                           | HOST `host_root` (= `/src` via bind mount)                | INSIDE container — SDK Bash rewritten to `docker exec …` AND/OR MCP `shell_in_container` → `secb-exec`    |
| C    | OpenAI / Anthropic / Qwen (LiteLLM inside OpenHands SDK)| HOST `host_root` (via OpenHands `FileEditorTool`)         | INSIDE container — MCP `shell_in_container` only (OpenHands has no TerminalTool registered)               |

---

## 8. Prompt injection paths

There is no "prompt is copied into the container" — the prompt is a string
passed to the LLM via the worker adapter. The container is just where the
LLM's *resulting actions* execute. Concretely:

1. `PromptBuilder` (Jinja2, `prompts/` tree: `system.j2` → `roles/` →
   `operations/` → `domains/secbench/*`) renders the worker prompt for the
   active operation (`assess_task` / `evaluate_task` / `execute_task` /
   flat). `SecBenchPromptStrategy` (`plugins/security/prompt_strategy.py`)
   attaches CVE-specific context (`domains/secbench/cve.j2`) and an
   operation-specific scaffold (e.g. `flat_pipeline.j2` for the flat mode).
2. `AgentOrchestrator.execute_task` (or `_run_flat_mode` for A) sets
   `task_context["task_description"] = rendered_prompt`.
3. The worker adapter delivers it to the LLM:
   - Cell A: `_build_argv` ends with `["--", rendered_prompt]` — the prompt
     is the trailing positional argument to the `claude` CLI; the CLI then
     does its own initial-turn handling
     (`infrastructure/workers/claude_code_worker.py:300`).
   - Cell B: `async with ClaudeSDKClient(options=options) as client:
     await client.query(task_description)` — the SDK sends it as the
     initial user message
     (`infrastructure/adapters/worker/claude_sdk_adapter.py:114`).
   - Cell C: `conversation.send_message(task_description)` followed by
     `executor.submit(conversation.run)` — the prompt becomes the seed
     user message and OpenHands' agent loop iterates from there
     (`infrastructure/adapters/worker/openhands_adapter.py:214`).
4. Before steps 3b/3c, both Claude-SDK and OpenHands paths prefix the
   prompt via `apply_task_prefix(...)`
   (`infrastructure/adapters/worker/shared/...`) — this inserts explicit
   host↔container-path translation rules and (for Claude SDK only,
   `auto_shell=True`) instructions on which Bash tool to use.
5. The LLM then issues tool calls (Read / Write / Edit / Bash /
   shell_in_container / valgrind_run / klee_run) that physically touch
   the bind-mounted host directories or the in-container shell.

For BOSS/MANAGER (B + C), prompts go via `LLMPort.query(prompt, config)`
through OpenRouter / LiteLLM, never via a worker container. Those agents
don't run `docker exec`, but they CAN read the host mirror of the CVE
source via `ReconToolAdapter` (which is rooted at `host_root` per
`execution_service.py:960`) — so they technically can observe the host
mirror, they just don't mutate it or shell into the container.

---

## 9. Event sourcing / CQRS boundary

- **Write side**: every behavior on `AgentSession`
  (`core/domain/aggregates/agent_session.py`) appends frozen Pydantic events
  to `PostgresEventStore`. OCC via unique `(aggregate_id, sequence_number)`.
- **Read side**: `ProjectionPipelineBuilder` (`core/query/projections.py`)
  rebuilds summaries from the event log after the run; per-run
  `events.jsonl` is also projected to disk for cross-tool analysis.
- The `query/` package (FastAPI + React SPA) is a separate read-side
  consumer — not in the run loop.

---

## 10. End-to-end legend (one box per "actor")

```
              ┌──────────────────────┐                                                  ┌────────────────────────────────┐
              │  Researcher           │                                                  │  External AI providers          │
              │  (CLI: scripts in     │                                                  │   • api.anthropic.com           │
              │  experiments/shared)  │                                                  │   • api.openai.com              │
              └──────┬───────────────┘                                                  │   • openrouter.ai/api/v1        │
                     │ `harness.run_arise()` per (cell, task, replicate)                  │   • api.ollama.com (cloud-qwen) │
                     ▼                                                                  └────────────────┬───────────────┘
              ┌───────────────────────────────────────────────────────────────────────┐                  │
              │  HOST process: `python main.py -c <cfg> run <task>` (one per replicate)│                  │
              │  ───────────────────────────────────────────────────────────────────  │                  │
              │  bootstrap → composition → infrastructure → application                │                  │
              │                                                                       │                  │
              │  ┌─────────────────┐    ┌───────────────────────┐    ┌──────────────┐ │                  │
              │  │ Boss / Manager   │   │ Verification (judge,   │   │ Worker adapter│ │ ───── HTTPS ─────┘
              │  │  via LLMPort     │   │ skip_judge=true/false) │   │ (Claude SDK / │ │
              │  │  (OpenRouter or  │   │  via LLMPort           │   │  OpenHands /  │ │
              │  │   LiteLLM)       │   │                        │   │  Claude CLI)  │ │
              │  └─────────────────┘    └───────────────────────┘    └───────┬──────┘ │
              │                                                              │        │
              │  ┌────────────────────── core/ event sourcing ────────────────┴──┐    │
              │  │  AgentSession aggregate → PostgresEventStore (localhost:5432) │    │
              │  └──────────────────────────────────────────────────────────────┘    │
              └────────────────────────────┬─────────────────────────────────────────┘
                                           │                                          
                                           │  spawns worker engine                    
                                           │  (A: `claude` CLI as docker-exec child;  
                                           │   B: native-async Claude SDK in-process; 
                                           │   C: OpenHands SDK on ThreadPoolExecutor)
                                           │   AND a stdio MCP subprocess             
                                           ▼                                          
              ┌───────────────────────────────────────────────────────────────────────┐
              │  Custom-tool MCP server (stdio subprocess)                            │
              │  `python -m plugins.security.mcp.security_tools_server`               │
              │   tools: shell_in_container, valgrind_run, klee_run                   │
              │                                                                       │
              │   Host mode (B/C): shells via `secb-exec` ──► docker exec             │
              │   In-container mode (A): runs directly inside container               │
              └──────────────────────────────┬────────────────────────────────────────┘
                                             │ docker exec / bind-mount
                                             ▼
              ┌───────────────────────────────────────────────────────────────────────┐
              │  CVE Docker container (one per agent_id)                              │
              │  Image: hwiwonlee/secb.eval.x86_64/<repo>.<cve>:patch                 │
              │         (or `secb-tools:<tag>` when security_tools_enabled is set)    │
              │  Mounts:                                                              │
              │    runs/<root_id>/src       ⇆  /src                                   │
              │    runs/<root_id>/testcase  ⇆  /testcase                              │
              │    runs/<root_id>           ⇆  <container_workspace_root>             │
              │  Long-running: `tail -f /dev/null`                                    │
              │  B/C: every build/test/valgrind/klee command arrives via `docker exec` │
              │       (sent by the host-mode MCP server through `secb-exec`).         │
              │  Cell A: ALSO runs the `claude` CLI itself via `docker exec` AND       │
              │          runs the MCP server inside the same container, where         │
              │          shell_in_container/valgrind_run/klee_run execute directly    │
              │          (`bash -lc …`) without a second `docker exec` hop.           │
              └───────────────────────────────────────────────────────────────────────┘
```
