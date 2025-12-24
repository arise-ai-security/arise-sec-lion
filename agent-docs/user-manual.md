# Arise Sec Lion - User Manual

## Prerequisites

- Docker and Docker Compose installed
- Environment file configured in `deployment/` directory:
  - For `local` profile: `.env` (copy from `.env.example`)
  - For `dev` profile: `.env.dev`

## Directory

All docker compose commands run from the `deployment/` directory:

```bash
cd deployment
```

## Profiles

| Profile | Description |
|---------|-------------|
| `local` | Local PostgreSQL database |
| `dev` | External Supabase database (no local DB) |
| `prod` | Production mode |
| `test` | Test database only |

---

## Starting Services

### Start with local database
```bash
docker compose --profile local up -d
```

### Start with external database (Supabase)
```bash
docker compose --profile dev up -d
```

### Rebuild containers after code changes
```bash
docker compose --profile dev up -d --build
```

---

## Stopping Services

### Stop all services (preserves data)
```bash
docker compose --profile dev down
```

### Stop and remove volumes (⚠️ deletes database)
```bash
docker compose --profile dev down -v
```

---

## Building the Frontend

```bash
docker compose --profile dev exec app-dev bash -c "cd /app/query/web && npm install && npm run build"
```

After building, restart the API to serve new static files:
```bash
docker compose --profile dev restart api-dev
```

---

## Running Tasks

### Run a task
```bash
docker compose --profile dev exec app-dev python main.py run "Your task description"
```

### Examples
```bash
docker compose --profile dev exec app-dev python main.py run "Create a fizzbuzz Python function"
docker compose --profile dev exec app-dev python main.py run "Fix CVE-2021-1234 in vulnerable.c"
```

---

## Viewing Results

### List all runs
```bash
docker compose --profile dev exec app-dev python main.py list
```

### View events for last run
```bash
docker compose --profile dev exec app-dev python main.py events
```

### View events for specific agent
```bash
docker compose --profile dev exec app-dev python main.py events <agent-id>
```

### View summary
```bash
docker compose --profile dev exec app-dev python main.py summary
```

---

## Running Tests

```bash
docker compose --profile dev exec app-dev uv run pytest
```

### With verbose output
```bash
docker compose --profile dev exec app-dev uv run pytest -v --tb=short
```

---

## Viewing Logs

### All services
```bash
docker compose --profile dev logs -f
```

### API only
```bash
docker compose --profile dev logs -f api-dev
```

---

## Web Dashboard

Access at: **http://localhost:8000**

API docs at: **http://localhost:8000/api/docs**

---

## Common Issues

### Port 8000 already in use
```bash
# Find and kill the process
lsof -i :8000
kill -9 <PID>

# Or stop old containers
docker ps -a | grep arise
docker stop <container-id> && docker rm <container-id>
```

### Database connection issues
```bash
# Check if database is healthy
docker compose --profile dev ps

# View database logs
docker compose --profile dev logs db
```

### Rebuild everything from scratch
```bash
docker compose --profile dev down -v
docker compose --profile dev up -d --build
```
