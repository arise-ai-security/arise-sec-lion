# User Manual

## Getting Started

### Prerequisites

- Docker and Docker Compose (v2+)
- A `.env` file in `deployment/` with non-secret local settings (copy from `.env.example`)
- Provider credentials injected into the shell from Bitwarden when a run needs them

### Building and Starting

```bash
cd deployment
docker compose --profile local up -d --build
```

This builds the application image and starts three containers:

| Container | Service | Purpose |
|-----------|---------|---------|
| `arise-app` | `app` | CLI entry point (sleeps until you exec into it) |
| `postgres-main` | `db` | Passwordless loopback-only PostgreSQL 16 + pgvector for all local data |
| `arise-api` | `api` | REST API + Dashboard on `http://localhost:8000` |

Wait for health checks to pass before running commands:

```bash
docker compose --profile local ps   # all should show "Up" / "healthy"
```

### Single local PostgreSQL policy

| Item | Local contract |
|---|---|
| Server | The only persistent PostgreSQL server is `postgres-main`. |
| Storage | Every logical database is under the bind mount `data/postgres`. |
| Host access | `127.0.0.1:5432` only, using PostgreSQL `trust`; no password. |
| Application access | Compose services connect to `db:5432` on the private Compose network. |
| Integration tests | Use `arise_test` inside `postgres-main`; never create another DB container. |
| Client tools | Run `psql`, `pg_dump`, and `pg_isready` with `docker exec postgres-main`; no host PostgreSQL installation is required. |

The test database is logically isolated because its fixture drops the `events` table during
cleanup. It still shares the same PostgreSQL server and persistent data root.

### Rebuilding After Code Changes

After modifying source code, rebuild the app container:

```bash
docker compose --profile local up -d --build app
```

To rebuild all services (e.g. after dependency changes):

```bash
docker compose --profile local up -d --build
```

---

## CLI Reference

The CLI runs inside the Docker container. Prefix every command with `docker compose exec`:

```bash
cd deployment
docker compose --profile local exec app python main.py <command> [options]
```

Or open a shell first and run commands directly:

```bash
docker compose --profile local exec app bash
# now inside the container:
python main.py <command> [options]
```

Global option `-c` / `--config` overrides the YAML config file (default: auto-loaded from `config/`).

### `run` — Execute a Task

```
python main.py run <task> [--worker-tool TOOL] [--worker-model MODEL]
                          [--domain DOMAIN] [--cve-file PATH]
```

| Argument / Option | Description |
|-------------------|-------------|
| `task` | Task description (positional, required) |
| `--worker-tool` | `claude_code`, `openhands`, `google_adk` |
| `--worker-model` | LLM model override (e.g. `gpt-4o`, `claude-sonnet-4-20250514`) |
| `--domain` | Enable domain plugin: `security` |
| `--cve-file` | SEC-bench CVE instance JSON (implies `--domain security`) |

#### Generic Tasks (No Plugin)

By default, the system operates as a **generic multi-agent orchestrator** with no security domain. Any task description works:

```bash
# Simple task — workers execute directly
docker compose --profile local exec app \
  python main.py run "Refactor the authentication module"

# Override the worker tool (default is set in config.yaml)
docker compose --profile local exec app \
  python main.py run "Fix memory leak in parser" --worker-tool claude_code

# Override the worker LLM model
docker compose --profile local exec app \
  python main.py run "Add pagination to the API" --worker-model claude-sonnet-4-20250514

# Combine both overrides
docker compose --profile local exec app \
  python main.py run "Migrate database schema" --worker-tool openhands --worker-model gpt-4o
```

The system decomposes the task (BOSS -> MANAGER -> WORKER), executes it, and writes results to the output directory.

#### Security Tasks (Plugin Enabled)

The security plugin activates **only** when explicitly requested. It adds CVE context inference, SEC-bench container lifecycle, and security-specific prompt strategies.

**Option A — CVE instance file** (implies `--domain security` automatically):

```bash
docker compose --profile local exec app \
  python main.py run "Patch CVE-2023-1234" --cve-file deployment/gpac_cve_instance.json
```

**Option B — Domain flag** (no CVE file, security prompts still active):

```bash
docker compose --profile local exec app \
  python main.py run "Analyze buffer overflow in libxml2" --domain security
```

Both approaches enable the same plugin — `--cve-file` just additionally loads the CVE context from the JSON file.

#### Worker Tools

| Tool | Flag | Description |
|------|------|-------------|
| Claude Code | `--worker-tool claude_code` | Claude Agent SDK with file/shell tools |
| OpenHands | `--worker-tool openhands` | OpenHands AI developer (default in config) |
| Google ADK | `--worker-tool google_adk` | Google ADK with Gemini + MCP filesystem tools |

### `list` — List Past Runs

```
python main.py list [--limit N] [--format text|json]
```

| Option | Default |
|--------|---------|
| `--limit` | `10` |
| `--format` | `text` |

### `events` — View Events

```
python main.py events [--agent-id UUID] [--format FORMAT] [--errors-only] [-o FILE]
```

| Option | Default |
|--------|---------|
| `--agent-id` | Last run |
| `--format` | `json` (also: `jsonl`, `text`, `compact`) |
| `--errors-only` | Off |
| `-o` / `--output` | stdout |

### `summary` — View Run Summary

```
python main.py summary [--agent-id UUID] [--format json|text]
```

### `prompts` — Trace Prompt Hierarchy

```
python main.py prompts [--agent-id UUID] [--format tree|json]
```

For `events`, `summary`, and `prompts`: `--agent-id` defaults to the last run.

---

## Dashboard (Web UI)

The React dashboard is served by the API container at `http://localhost:8000`.

### Starting the API + Dashboard

```bash
cd deployment
docker compose --profile local up -d --build   # starts app + api + postgres
```

The API server runs via `uvicorn query.api.bootstrap:create_app` on port 8000. Swagger docs at `http://localhost:8000/api/docs`.

### Local Frontend Development

```bash
cd query/web
npm install
npm run dev    # Vite dev server on http://localhost:5173
```

CORS is pre-configured for `localhost:5173` and `localhost:3000` in `config.yaml`.

---

## Plugins

### Security Plugin

Activates when `--domain security` or `--cve-file` is passed to `run`.

**What it provides:**
- CVE context inference from task description or JSON instance file
- SEC-bench container lifecycle management (Docker-based)
- Security-specific prompt strategy for agent decomposition
- Configurable analysis tools inside containers

**Configuration** (`config.yaml`):

```yaml
security:
  enabled: true
  tools: [valgrind, klee]   # tools available in SEC-bench containers
```

**Enabling/disabling:**
- Set `security.enabled: false` in config to disable the plugin entirely
- Omit `--domain` / `--cve-file` from the `run` command to skip it per-run

---

## Recon (Tool-Calling Reconnaissance)

Recon is an automatic capability — not a CLI command. During PENDING/MANAGER assessment, agents can call tools (`search_codebase`, `read_file`, `get_symbols_overview`, `read_symbol`, `get_file_structure`) to gather context before deciding to execute or decompose.

### Enabling / Disabling

Per-role in `config.yaml` under `orchestration.tool_calling.policies`:

```yaml
orchestration:
  tool_calling:
    policies:
      default:
        pending:
          toolsets:
            recon: { enabled: false }
        manager:
          toolsets:
            recon: { enabled: false }
        boss:
          toolsets:
            recon: { enabled: false }
```

Set `enabled: true` on any role to enable recon for that role.

### Per-Domain Overrides

Domain-specific policies override defaults:

```yaml
      domains:
        secbench:
          manager:
            max_iterations: 3
            toolsets:
              recon:
                allowed_tools:
                  - search_codebase
                  - read_file
                  - get_file_structure
                  - get_symbols_overview
                  - read_symbol
```

### Context Management

Prevents context overflow during tool-calling loops:

```yaml
orchestration:
  tool_calling:
    max_iterations: 5              # hard cap on tool-calling rounds
    result_char_limit: 6000        # truncate each tool result
    condense_after_iteration: 2    # LLM-summarize older exchanges
    token_budget: 80000            # force-stop if exceeded
```

---

## Experiments

The confirmatory security study compares the same SEC-bench task under two complete
systems. **N1 is the control:** one naive OpenHands session, OpenHands subagents disabled,
and `orchestration.procedural_dispatch: false`. **B4 is the treatment:** BOSS, phase
Managers, compact role workers, and four Host-owned mechanical procedures with one bounded
repair/recheck path. N1 may run `secb build`, `secb repro`, and `secb patch` itself; the Host
must not run those procedures for N1 while it is solving.

| Comparison dimension | N1 control | B4 treatment | Held constant? |
|----------------------|------------|--------------|----------------|
| Task | Build → Exploit → Fix → Report | Build → Exploit → Fix → Report | Yes |
| Required evidence | Same canonical `/testcase` artifacts, including root-cause analysis and PatchPlan | Same canonical `/testcase` artifacts | Yes |
| Solving system | One flat OpenHands session; no subagents or Host procedures | Hierarchy, role/model routing, Host procedures, bounded recovery | No — this is the treatment bundle |
| Post-run measurement | Six fresh replays, safety/provenance, frozen Host regression, blinded semantic panel | Identical evaluator | Yes |

Success is arm-independent and conjunctive:

| Required gate | Success criterion |
|---------------|-------------------|
| Common artifacts (mechanical subgate) | Every shared deliverable is present and non-vacuous in both arms, except `repo_changes.diff` may be empty; `patch_plan.json` must satisfy the strict common schema |
| Mechanical replay | PoC reproduces the exact frozen crash signature in 3/3 fresh containers; the patch applies, builds, and passes the primary patched replay in 3/3 fresh containers |
| Safety and provenance | Artifacts are sealed and hashed; protected paths are untouched; all six containers are distinct; pre/post task and base identity match; patched executions have no timeout, signal, assertion abort, core dump, or sanitizer finding |
| Host regression | The preregistered arm-independent regression plan is available and every required command passes in a fresh patched container |
| Semantic | All three blinded judge seats return valid votes and at least two accept the causal analysis, repair layer, evidence consistency, scope, and regression risk |
| Final | `mechanical AND safety AND host regression AND semantic` |

The external evaluator is measurement equipment, not N1 assistance: it runs only after the
agent session ends and uses the same frozen contract for both arms. Therefore N1 versus B4
estimates the effect of the **complete B4 system**, not hierarchy alone. Use B3 for the
Manager-layer ablation.

See the diagram-first [system overview](docs/system-overview.html), the standalone
[N1 vs B4 experiment design report](docs/n1-b4-experiment-report.html), and
[EXPERIMENT_MANUAL.md](EXPERIMENT_MANUAL.md) for operating commands.

---

## Docker Profiles

| Profile | Database | Use Case |
|---------|----------|----------|
| `local` | Local PostgreSQL container | Development with local DB |
| `dev` | External DB (e.g. Supabase) | Development with cloud DB |
| `prod` | External DB | Production |
| `test` | Ephemeral PostgreSQL (tmpfs) | Tests |

All CLI commands are prefixed with `docker compose --profile <profile> exec app`:

```bash
docker compose --profile local exec app python main.py run "Your task"
docker compose --profile local exec app python main.py list
docker compose --profile local exec app python main.py events --errors-only
```

### Running Tests

```bash
docker compose --profile local exec app uv run pytest
docker compose --profile local exec app uv run pytest -v --tb=short
docker compose --profile local exec app uv run pytest core/domain/tests/
```
