# Configuration

## Load Priority (highest wins)

1. **Environment variables** (secrets only: `POSTGRES_PASSWORD`, `OPENAI_API_KEY`, etc.)
2. **Environment-specific YAML** (`config/config.{ARISE_ENV}.yaml`)
3. **Base YAML** (`config/config.yaml`)
4. **Code defaults** (Pydantic models in `config/settings.py`)

## Settings Structure

```python
Settings                    # Root (Pydantic BaseSettings)
├── database: DatabaseConfig    # host, port, user, password (env), name
├── boss: BossConfig            # model, temperature, max_tokens
├── manager: ManagerConfig      # model, temperature, max_tokens
├── worker: WorkerConfig        # model, tool (claude_code|openhands|google_adk), timeout
├── orchestration: OrchestrationConfig
│   ├── max_retries, poll_interval, decomposition_strategy, global_budget_usd
│   ├── limits: LimitsConfig
│   │   └── max_depth, max_children_per_node, max_total_agents,
│   │       max_concurrent_workers, max_concurrent_llm_calls, llm_jitter_max_ms
│   └── retry: RetryConfig
│       └── model_escalation_chain, retry_budget_fraction,
│           circuit_breaker_threshold, circuit_breaker_reset_seconds
├── output: OutputConfig        # verbose, show_progress, log_level, directory
├── security: SecurityConfig    # enabled
└── cors: CorsConfig            # allowed_origins, methods, headers
```

## Secrets

Secrets live in `deployment/.env` (never committed):

```bash
POSTGRES_PASSWORD=...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

Rules:
- NEVER put secrets in `config/*.yaml`
- NEVER put application config in `.env`

## Environment Phases

Set via `ARISE_ENV` (default: `development`):

| Phase | File | Purpose |
|-------|------|---------|
| `development` | `config.development.yaml` | Cheaper models, verbose |
| `production` | `config.production.yaml` | Best models, minimal logging |

## Docker Compose Profiles

| Profile | Description |
|---------|-------------|
| `local` | Local PostgreSQL (safe default — **always use this**) |
| `dev` | External Supabase DB |
| `prod` | Production mode |
| `test` | Test DB only |
