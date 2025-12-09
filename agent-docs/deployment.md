# Deployment Guide

This guide covers the complete deployment architecture for the Arise Multi-Agent System.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER / BROWSER                                 │
│                                    │                                        │
│                    ┌───────────────┴───────────────┐                       │
│                    ▼                               ▼                        │
│             http://localhost:3000          http://localhost:8000            │
│                    │                               │                        │
└────────────────────┼───────────────────────────────┼────────────────────────┘
                     │                               │
┌────────────────────┼───────────────────────────────┼────────────────────────┐
│                    │     Docker Network            │                        │
│                    │     (arise-network)           │                        │
│                    ▼                               ▼                        │
│  ┌─────────────────────────┐       ┌─────────────────────────┐             │
│  │         WEB             │       │          API            │             │
│  │    (arise-web)          │       │      (arise-api)        │             │
│  │                         │       │                         │             │
│  │  ┌───────────────────┐  │       │  ┌───────────────────┐  │             │
│  │  │  React Dashboard  │  │       │  │  FastAPI Server   │  │             │
│  │  │  (Static Build)   │  │       │  │                   │  │             │
│  │  └───────────────────┘  │       │  │  /api/agents      │  │             │
│  │           │             │       │  │  /api/events      │  │             │
│  │  ┌───────────────────┐  │       │  │  /api/config      │  │             │
│  │  │      Nginx        │──┼──────▶│  │  /api/prompts     │  │             │
│  │  │  (Reverse Proxy)  │  │ /api/ │  │  /api/health      │  │             │
│  │  └───────────────────┘  │       │  └───────────────────┘  │             │
│  │                         │       │           │             │             │
│  └─────────────────────────┘       └───────────┼─────────────┘             │
│              │                                 │                            │
│              │                                 │                            │
│              │         ┌─────────────────────────────────────┐             │
│              │         │            APP                      │             │
│              │         │        (arise-app)                  │             │
│              │         │                                     │             │
│              │         │  ┌─────────────────────────────┐   │             │
│              │         │  │    CLI Application          │   │             │
│              │         │  │    (python main.py run)     │   │             │
│              │         │  └─────────────────────────────┘   │             │
│              │         │               │                     │             │
│              │         │  ┌────────────┴────────────┐       │             │
│              │         │  │    Worker Adapters      │       │             │
│              │         │  │  ┌──────┐  ┌─────────┐  │       │             │
│              │         │  │  │Claude│  │OpenHands│  │       │             │
│              │         │  │  │ Code │  │   SDK   │  │       │             │
│              │         │  │  └──────┘  └─────────┘  │       │             │
│              │         │  └─────────────────────────┘       │             │
│              │         │               │                     │             │
│              │         │     ┌─────────┴─────────┐          │             │
│              │         │     ▼ Docker Socket     │          │             │
│              │         │  (Sibling Containers)   │          │             │
│              │         └─────────────────────────────────────┘             │
│              │                         │                                    │
│              │                         │                                    │
│              │         ┌───────────────┴───────────────┐                   │
│              │         ▼                               ▼                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                           DB (arise-db)                             │   │
│  │                                                                     │   │
│  │                    PostgreSQL 16 Alpine                             │   │
│  │                                                                     │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │                    events table                             │   │   │
│  │  │  (aggregate_id, sequence_number, event_type, payload, ...)  │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                     │   │
│  │  Volume: postgres_data                                              │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Services Overview

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| **web** | arise-web | 3000:80 | React dashboard served by Nginx |
| **api** | arise-api | 8000:8000 | FastAPI REST API + SSE streaming |
| **app** | arise-app | - | CLI for running agent tasks |
| **db** | arise-db | 5432:5432 | PostgreSQL event store |

## Docker Compose Configuration

### Development (`docker-compose.yml`)

```yaml
services:
  # ═══════════════════════════════════════════════════════════════════════════
  # WEB - React Dashboard
  # ═══════════════════════════════════════════════════════════════════════════
  web:
    build:
      context: ../presentation/web    # Build from web directory
      dockerfile: Dockerfile          # Multi-stage: node → nginx
    container_name: arise-web
    ports:
      - "3000:80"                      # Nginx serves on port 80
    depends_on:
      api:
        condition: service_healthy    # Wait for API to be ready
    networks:
      - arise-network
    restart: unless-stopped
```

**What it does:**
- Builds React app with Vite
- Serves static files via Nginx
- Proxies `/api/*` requests to the API container
- Supports SSE (Server-Sent Events) for real-time updates

---

```yaml
  # ═══════════════════════════════════════════════════════════════════════════
  # API - FastAPI Server
  # ═══════════════════════════════════════════════════════════════════════════
  api:
    build:
      context: ..                       # Project root
      dockerfile: deployment/Dockerfile
      target: development               # Use dev stage with hot reload
    container_name: arise-api
    volumes:
      - ..:/app                         # Mount source for hot reload
      - /app/.venv                      # Exclude venv from mount
    depends_on:
      db:
        condition: service_healthy
    env_file: .env                      # Load secrets
    environment:
      ARISE_ENV: ${ARISE_ENV:-development}
      ARISE_INFRA_POSTGRES_HOST: db     # Override for Docker networking
      ARISE_INFRA_POSTGRES_USER: ${POSTGRES_USER}
      ARISE_INFRA_POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      ARISE_INFRA_POSTGRES_DATABASE: ${POSTGRES_DB}
    ports:
      - "8000:8000"
    networks:
      - arise-network
    command: uvicorn presentation.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
```

**What it does:**
- Runs FastAPI with uvicorn and hot reload
- Provides REST endpoints for dashboard
- Streams real-time events via SSE
- Health check ensures proper startup order

---

```yaml
  # ═══════════════════════════════════════════════════════════════════════════
  # APP - CLI Application
  # ═══════════════════════════════════════════════════════════════════════════
  app:
    build:
      context: ..
      dockerfile: deployment/Dockerfile
      target: development
    container_name: arise-app
    volumes:
      - ..:/app                                    # Mount source
      - /app/.venv                                 # Exclude venv
      - /var/run/docker.sock:/var/run/docker.sock # Docker-in-Docker
    depends_on:
      db:
        condition: service_healthy
    env_file: .env
    environment:
      ARISE_ENV: ${ARISE_ENV:-development}
      ARISE_INFRA_POSTGRES_HOST: db
      ARISE_INFRA_POSTGRES_USER: ${POSTGRES_USER}
      ARISE_INFRA_POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      ARISE_INFRA_POSTGRES_DATABASE: ${POSTGRES_DB}
    networks:
      - arise-network
    command: sleep infinity                        # Keep running for exec
    stdin_open: true
    tty: true
```

**What it does:**
- Runs CLI commands interactively
- Mounts Docker socket for OpenHands sibling containers
- Stays alive for `docker compose exec` commands

---

```yaml
  # ═══════════════════════════════════════════════════════════════════════════
  # DB - PostgreSQL Event Store
  # ═══════════════════════════════════════════════════════════════════════════
  db:
    image: postgres:16-alpine
    container_name: arise-db
    env_file: .env
    ports:
      - "${POSTGRES_PORT:-5432}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data    # Persist data
      - ../infrastructure/sql/create_events_table.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - arise-network
```

**What it does:**
- Stores all domain events (event sourcing)
- Auto-initializes schema on first run
- Persists data via Docker volume

---

## Build Stages (Backend Dockerfile)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Stage 1: builder                                                           │
│  ─────────────────                                                          │
│  FROM python:3.12-slim                                                      │
│                                                                             │
│  • Install uv (fast package manager)                                        │
│  • Copy pyproject.toml, uv.lock                                            │
│  • Install production dependencies only                                     │
│                                                                             │
│  Purpose: Create clean venv for production stage                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
          ┌─────────────────────────┴─────────────────────────┐
          ▼                                                   ▼
┌─────────────────────────────────────┐   ┌─────────────────────────────────────┐
│  Stage 2: development               │   │  Stage 3: production                │
│  ────────────────────               │   │  ───────────────────                │
│  FROM python:3.12-slim              │   │  FROM python:3.12-slim              │
│                                     │   │                                     │
│  • Install Node.js 20.x             │   │  • Minimal dependencies (git only)  │
│  • Install Claude Code CLI          │   │  • Non-root user (appuser)          │
│  • Install ALL deps (incl. dev)     │   │  • Copy venv from builder           │
│  • Hot reload enabled               │   │  • Optimized for size               │
│                                     │   │                                     │
│  Used by: api, app containers       │   │  Used by: production deployment     │
│  Target: development                │   │  Target: production                 │
└─────────────────────────────────────┘   └─────────────────────────────────────┘
```

## Build Stages (Web Dockerfile)

```
┌─────────────────────────────────────┐   ┌─────────────────────────────────────┐
│  Stage 1: builder                   │   │  Stage 2: production                │
│  ─────────────────                  │   │  ───────────────────                │
│  FROM node:20-alpine                │   │  FROM nginx:alpine                  │
│                                     │   │                                     │
│  • npm ci (install deps)            │──▶│  • Copy nginx.conf                  │
│  • npm run build (Vite)             │   │  • Copy dist/ from builder          │
│  • Output: dist/                    │   │  • Serve static files               │
│                                     │   │                                     │
│  Size: ~500MB (with node_modules)   │   │  Size: ~25MB (nginx + static)       │
└─────────────────────────────────────┘   └─────────────────────────────────────┘
```

## Nginx Configuration

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;

    # ─────────────────────────────────────────────────────────────────────────
    # API Proxy - Forward /api/* to FastAPI backend
    # ─────────────────────────────────────────────────────────────────────────
    location /api/ {
        proxy_pass http://api:8000/api/;    # Docker service name resolution

        # SSE Support (Server-Sent Events)
        proxy_buffering off;                 # Disable buffering for streaming
        proxy_cache off;
        proxy_set_header Connection '';
        chunked_transfer_encoding off;

        # Long timeouts for SSE connections
        proxy_read_timeout 86400s;           # 24 hours
        proxy_send_timeout 86400s;
    }

    # ─────────────────────────────────────────────────────────────────────────
    # Static Assets - Cache for 1 year (hashed filenames)
    # ─────────────────────────────────────────────────────────────────────────
    location /assets/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # ─────────────────────────────────────────────────────────────────────────
    # SPA Fallback - Serve index.html for client-side routing
    # ─────────────────────────────────────────────────────────────────────────
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

## Environment Variables

Create `.env` from `.env.example`:

```bash
# ═══════════════════════════════════════════════════════════════════════════
# Database Credentials
# ═══════════════════════════════════════════════════════════════════════════
POSTGRES_USER=arise
POSTGRES_PASSWORD=arise_secret          # Change in production!
POSTGRES_DB=arise_events

# ═══════════════════════════════════════════════════════════════════════════
# LLM Provider API Keys
# ═══════════════════════════════════════════════════════════════════════════
OPENAI_API_KEY=sk-your-openai-key       # Required for default config

# Optional: Only if using Claude Code worker
# ANTHROPIC_API_KEY=sk-ant-your-key
```

## Startup Sequence

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. Database (db)                                                           │
│     ┌─────────────────────────────────────────────────────────────────┐    │
│     │  • Start PostgreSQL                                             │    │
│     │  • Run /docker-entrypoint-initdb.d/01-schema.sql               │    │
│     │  • Health check: pg_isready                                     │    │
│     └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│                                    ▼ healthy                                │
│  2. API (api)                                                               │
│     ┌─────────────────────────────────────────────────────────────────┐    │
│     │  • Start uvicorn with FastAPI                                   │    │
│     │  • Connect to PostgreSQL                                        │    │
│     │  • Health check: GET /api/health                                │    │
│     └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│                                    ▼ healthy                                │
│  3. Web (web)                                                               │
│     ┌─────────────────────────────────────────────────────────────────┐    │
│     │  • Start Nginx                                                  │    │
│     │  • Serve React static build                                     │    │
│     │  • Proxy /api/ to API container                                 │    │
│     └─────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  4. App (app) - Runs independently                                          │
│     ┌─────────────────────────────────────────────────────────────────┐    │
│     │  • Wait for db to be healthy                                    │    │
│     │  • Run `sleep infinity` (stays alive for exec)                  │    │
│     │  • Ready for: docker compose exec app python main.py run "..."  │    │
│     └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
cd deployment

# 1. Setup environment
cp .env.example .env
# Edit .env with your API keys

# 2. Build and start all services
docker compose up --build -d

# 3. Verify services are running
docker compose ps

# 4. View logs
docker compose logs -f api     # API logs
docker compose logs -f web     # Web logs
docker compose logs -f app     # CLI logs

# 5. Access services
# Dashboard:  http://localhost:3000
# API Docs:   http://localhost:8000/api/docs
# API Health: http://localhost:8000/api/health

# 6. Run a task
docker compose exec app python main.py run "Build a hello world script"

# 7. View results
docker compose exec app python main.py events
docker compose exec app python main.py summary
```

## Production Deployment

For production, use `docker-compose.prod.yml`:

```bash
# Build production images
docker compose -f docker-compose.prod.yml build

# Start production services
docker compose -f docker-compose.prod.yml up -d
```

**Production differences:**
- Uses `production` Dockerfile target (smaller image, non-root user)
- No hot reload or mounted volumes
- Database port not exposed externally
- `restart: unless-stopped` for all services

## Network Flow

| From | To | Path | Protocol |
|------|-----|------|----------|
| Browser | web | `:3000` | HTTP |
| web (nginx) | api | `/api/*` → `:8000` | HTTP (internal) |
| api | db | `:5432` | PostgreSQL |
| app | db | `:5432` | PostgreSQL |
| app | Host Docker | `/var/run/docker.sock` | Unix socket |

## Volume Mounts

| Volume | Container | Path | Purpose |
|--------|-----------|------|---------|
| `postgres_data` | db | `/var/lib/postgresql/data` | Persist database |
| Source code | api, app | `/app` | Hot reload (dev only) |
| Docker socket | app | `/var/run/docker.sock` | OpenHands sibling containers |

## Troubleshooting

### Database connection failed
```bash
# Check if db is healthy
docker compose ps db

# View db logs
docker compose logs db

# Restart db
docker compose restart db
```

### API not responding
```bash
# Check health endpoint
curl http://localhost:8000/api/health

# View API logs
docker compose logs api

# Rebuild API container
docker compose up --build api
```

### Web dashboard blank
```bash
# Check if build succeeded
docker compose logs web

# Rebuild web container
docker compose up --build web
```

### Worker tool errors
```bash
# Check API keys in .env
cat deployment/.env | grep API_KEY

# Verify environment in container
docker compose exec app env | grep OPENAI
```
