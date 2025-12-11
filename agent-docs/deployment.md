# Deployment Guide

## Quick Start

```bash
cd deployment
cp .env.example .env     # Add your API keys
docker compose up -d     # Start everything

# Run a task
docker compose exec app python main.py run "Your task"

# View results
docker compose exec app python main.py events
docker compose exec app python main.py summary
```

Dashboard: `cd presentation/web && npm run dev` → http://localhost:5173

## Services

| Service | Port | Purpose |
|---------|------|---------|
| api | 8000 | FastAPI REST + SSE |
| app | - | CLI for tasks |
| db | 5432 | PostgreSQL |

## Configuration

**Environment vars override everything.** Set in `deployment/.env`:

```bash
# Required
POSTGRES_USER=arise
POSTGRES_PASSWORD=arise_secret
POSTGRES_DB=arise
OPENAI_API_KEY=sk-your-key

# Optional overrides (prefix with ARISE_INFRA_, ARISE_APP_, or ARISE_UI_)
# ARISE_INFRA_LLM_MODEL_BOSS=claude-3-5-sonnet-20241022
# ARISE_INFRA_WORKER_TOOL_TYPE=claude_code
# ARISE_APP_WORKER_TIMEOUT=600
```

Defaults are in `config/config.yaml`.

## Profiles

```bash
docker compose up -d                    # Development (default)
docker compose --profile prod up -d     # Production (minimal image)
docker compose --profile test up -d     # Test DB only (port 5433)
```

## Troubleshooting

```bash
docker compose ps           # Check status
docker compose logs api     # View logs
docker compose restart db   # Restart service
curl localhost:8000/api/health  # Test API
```
