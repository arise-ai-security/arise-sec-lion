# Configuration

This document explains the configuration system following 12-Factor App principles.

---

## Separation of Config and Secrets

| Category | Location | Committed to Git? |
|----------|----------|-------------------|
| Secrets (passwords, API keys) | `deployment/.env` | Never |
| Docker overrides (hostnames) | `deployment/.env` | No |
| Base defaults | `config/config.yaml` | Yes |
| Environment overrides | `config/config.{env}.yaml` | Yes |

---

## Load Hierarchy

Settings are loaded with this priority (highest to lowest):

1. **Environment variables** (secrets + Docker overrides only)
2. **Environment-specific YAML** (`config/config.{ARISE_ENV}.yaml`)
3. **Base YAML** (`config/config.yaml`)
4. **Code defaults** (in Pydantic models)

---

## Environment Phases

Set via `ARISE_ENV` environment variable:

| Phase | File | Purpose |
|-------|------|---------|
| `development` (default) | `config.development.yaml` | Cheaper models, verbose logging |
| `production` | `config.production.yaml` | Best models, minimal logging |

---

## Config File Examples

### Base Config (`config/config.yaml`)

```yaml
infrastructure:
  postgres_user: arise
  postgres_db: arise_events
  llm_model_boss: gpt-4o
  worker_tool_type: claude_code
  worker_tool_timeout: 300

application:
  max_retries: 3
  poll_interval: 0.5
  default_task_complexity_threshold: 3

output:
  directory: ./output
  verbose: true

cors:
  allowed_origins:
    - "http://localhost:5173"
    - "http://localhost:3000"
```

### Development Override (`config/config.development.yaml`)

```yaml
infrastructure:
  llm_model_boss: gpt-4o-mini  # Cheaper for dev

output:
  verbose: true
  log_level: DEBUG
```

### Production Override (`config/config.production.yaml`)

```yaml
cors:
  allowed_origins:
    - "https://arise.example.com"

output:
  verbose: false
  log_level: WARNING
```

---

## Secrets File (`deployment/.env`)

```bash
# Database credentials
POSTGRES_PASSWORD=your_secure_password
POSTGRES_HOST=db  # Docker service name

# API keys
OPENAI_API_KEY=sk-xxx
ANTHROPIC_API_KEY=sk-ant-xxx

# Optional: Set environment phase
# ARISE_ENV=production
```

Use `deployment/.env.example` as a template.

---

## Rules

1. **NEVER** put secrets in `config/*.yaml` files
2. **NEVER** put application config in `.env` (only secrets)
3. Use flat env var names for secrets: `POSTGRES_PASSWORD`, not `ARISE_POSTGRES_PASSWORD`
4. Config values can reference env vars in YAML if needed

---

## Settings Models

Defined in `config/settings.py`:

```python
class Settings(BaseSettings):
    infrastructure: InfrastructureConfig
    application: ApplicationConfig
    output: OutputConfig
    cors: CorsConfig

    @classmethod
    def load(cls) -> "Settings":
        """Load from YAML files based on ARISE_ENV."""
        ...
```

### Accessing Config

```python
from config import Settings

settings = Settings.load()
print(settings.infrastructure.llm_model_boss)  # "gpt-4o-mini"
print(settings.application.max_retries)        # 3
```

---

## Docker Compose Profiles

| Profile | Env File | Description |
|---------|----------|-------------|
| `local` | `.env` | Local PostgreSQL |
| `dev` | `.env.dev` | External Supabase DB |
| `prod` | `.env` | Production mode |
| `test` | (none) | Test DB with hardcoded creds |

```bash
# Development with Supabase
docker compose --profile dev up -d

# Local with PostgreSQL container
docker compose --profile local up -d
```
