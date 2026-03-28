# User Manual

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
