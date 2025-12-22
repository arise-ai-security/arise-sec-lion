# Deployment Guide

## Quick Start (Local)

```bash
cd deployment
cp .env.example .env     # Add your API keys
docker compose up -d     # Start everything (local DB)

# Run a task
docker compose exec app python main.py run "Your task"

# View results
docker compose exec app python main.py events
docker compose exec app python main.py summary
```

Dashboard: `cd presentation/web && npm run dev` → http://localhost:5173

## Quick Start (Dev - Shared Supabase DB)

```bash
cd deployment
cp .env.dev .env.dev.local   # Copy and edit with your credentials
# Edit .env.dev with your Supabase password

docker compose --profile dev up -d   # Start with external DB

# Run a task
docker compose exec app-dev python main.py run "Your task"
```

## Services

| Service | Port | Purpose |
|---------|------|---------|
| api | 8000 | FastAPI REST + SSE |
| app | - | CLI for tasks |
| db | 5432 | PostgreSQL (local only) |

## Profiles

| Profile | Command | Database | Use Case |
|---------|---------|----------|----------|
| (default) | `docker compose up -d` | Local PostgreSQL | Solo development |
| dev | `docker compose --profile dev up -d` | Supabase (external) | Team development |
| prod | `docker compose --profile prod up -d` | External | Production |
| test | `docker compose --profile test up -d` | tmpfs (ephemeral) | Testing |

## Configuration

**Environment vars override YAML config.**

### Local Development (`deployment/.env`)
```bash
POSTGRES_PASSWORD=arise_secret
OPENAI_API_KEY=sk-your-key
```

### Dev/Shared Database (`deployment/.env.dev`)
```bash
ARISE_ENV=dev
POSTGRES_HOST=db.your-project.supabase.co
POSTGRES_PORT=6543
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your-supabase-db-password
POSTGRES_DB=postgres
OPENAI_API_KEY=sk-your-key
```

Defaults are in `config/config.yaml`, phase overrides in `config/config.{env}.yaml`.

## Initialize Supabase Schema

Run the schema SQL on your Supabase database:

```bash
# Via psql
psql "postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:6543/postgres" \
  -f infrastructure/sql/create_events_table.sql

# Or via Supabase SQL Editor:
# Copy contents of infrastructure/sql/create_events_table.sql and run
```

## Troubleshooting

```bash
docker compose ps              # Check status
docker compose logs api        # View logs (local)
docker compose logs api-dev    # View logs (dev profile)
docker compose restart db      # Restart service
curl localhost:8000/api/health # Test API
```
