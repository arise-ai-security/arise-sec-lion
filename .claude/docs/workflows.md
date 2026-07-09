<!-- Read this when: working on build, deploy, CI/CD, or branch strategy -->
This file covers local development setup, Docker profiles, build steps, code quality checks, pre-commit hooks, branch strategy, SEC-bench tooling, and database schema initialization.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.12 | System or pyenv |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker + Compose | v2+ | Docker Desktop or Docker Engine |
| Node.js | 20+ | Only needed for frontend dev outside Docker (Dockerfile installs Node 20; Docker `web` service uses `node:22-slim`) |
| pre-commit | latest | `uv tool install pre-commit` or `pip install pre-commit` |

## Initial Setup

### 1. Install Python dependencies

```bash
uv sync --frozen
```

This installs all dependencies (including dev) from the lockfile `uv.lock`. The virtualenv is created at `.venv/`.

### 2. Install pre-commit hooks

```bash
uv run pre-commit install
```

Hooks run automatically on every commit. See `.pre-commit-config.yaml` for the full list.

### 3. Configure environment

```bash
cp deployment/.env.example deployment/.env
```

Edit `deployment/.env` and set at minimum:

| Variable | Required | Notes |
|----------|----------|-------|
| `POSTGRES_PASSWORD` | Yes | Any secure string for local DB |
| `OPENAI_API_KEY` | Yes | OpenAI API key for LLM calls |
| `ANTHROPIC_API_KEY` | No | Only if using Claude-based workers; otherwise use `claude login` |
| `HOST_PROJECT_ROOT` | No | Absolute host path to repo root; needed for Docker-out-of-Docker volume mapping in SEC-bench |
| `ARISE_ENV` | No | Config overlay: `development` (default), `dev`, `production` |

### 4. Install frontend dependencies (optional)

```bash
cd query/web && npm install
```

Only needed if developing the React dashboard outside Docker.

## Docker Profiles

All compose commands use `deployment/docker-compose.yml`. Run from the repo root with `-f deployment/docker-compose.yml` or from the `deployment/` directory.

| Profile | Command | Services | Database | Use Case |
|---------|---------|----------|----------|----------|
| `local` | `docker compose --profile local up -d` | `app` + `api` + `db` + `web` | Local PostgreSQL 16 on port 5432 | Day-to-day development |
| `dev` | `docker compose --profile dev up -d` | `app-dev` + `api-dev` | External Supabase (configured in `.env.dev`) | Shared dev environment |
| `prod` | `docker compose --profile prod up -d` | `app-prod` + `db` | Local PostgreSQL 16 | Production deployment |
| `test` | `docker compose --profile test up -d` | `test-db` | Ephemeral tmpfs PostgreSQL on port 5433 | Integration tests, data lost on stop |

### Local Profile Details

The `local` profile starts four containers:

- **`arise-db`** -- PostgreSQL 16 Alpine. Schema auto-created from `infrastructure/sql/create_events_table.sql` via `docker-entrypoint-initdb.d`. Data persisted in the `postgres_data` Docker volume. Exposed on host port `${POSTGRES_PORT:-5432}`.
- **`arise-api`** -- FastAPI server on port 8000. Entry: `uvicorn query.api.bootstrap:create_app --factory --host 0.0.0.0 --port 8000`. Health check at `/api/health`.
- **`arise-app`** -- Long-running container (`sleep infinity`) for executing CLI commands interactively. Mounts the Docker socket for Docker-out-of-Docker SEC-bench execution. Uses `init: true` (tini) to reap zombie processes.
- **`arise-web`** -- Node 22 slim container running the React/Vite frontend (`npm run dev -- --host 0.0.0.0`). Mounts `query/web` as a volume. No host port mapping by default; access via container networking or add a `ports:` entry.

Both `app` and `api` mount the repo as a volume at `/app` (with `.venv` excluded via anonymous volume), so code changes are reflected without rebuilding.

### Dockerfile Stages

File: `deployment/Dockerfile`. Multi-stage build:

| Stage | Base | Includes | Used By |
|-------|------|----------|---------|
| `development` | `python:3.12-slim` | Node.js 20, Claude Code CLI, Docker CE CLI, uv, all Python deps (including dev) | `local` and `dev` profiles |
| `production` | `python:3.12-slim` | uv, Python deps only (`--no-dev`), git | `prod` profile |

Both stages create a non-root `appuser` (UID 1000) and run as that user.

To rebuild images after dependency changes:

```bash
docker compose --profile local build
```

## Running Tasks

### CLI Commands

All CLI commands route through `main.py` which delegates to `bootstrap.bootstrap.main()` (Click-based).

```bash
# Inside the app container:
docker compose --profile local exec app python main.py run "Build a snake game"

# SEC-bench CVE task:
docker compose --profile local exec app python main.py run \
  --cve-file deployment/njs-cve-2022-28049.json --domain security

# Other CLI commands:
docker compose --profile local exec app python main.py list       # List past BOSS runs
docker compose --profile local exec app python main.py events     # View events for a run
docker compose --profile local exec app python main.py summary    # Show summary projection
docker compose --profile local exec app python main.py prompts    # Inspect prompt templates
```

### API Server (standalone, outside Docker)

```bash
uvicorn query.api.bootstrap:create_app --factory --host 0.0.0.0 --port 8000
```

Requires `POSTGRES_PASSWORD` and database connection env vars to be set.

### Frontend Dev Server

```bash
cd query/web && npm run dev
```

Runs on port 5173 with a proxy to the API at port 8000. CORS is configured in `config/config.yaml` to allow `localhost:5173` and `localhost:3000`.

## Code Quality Checks

No CI/CD pipeline exists. All checks must be run locally. Run them in this order:

### 1. Architecture boundary check

```bash
python3 scripts/check_architecture_boundaries.py
```

Enforces three rules (see `scripts/check_architecture_boundaries.py`):
- `core/` must never import from `infrastructure/`
- OpenHands SDK imports (`openhands`, `CmdOutputMetadata`, `Observation: kind=`) are forbidden in `core/` (they belong in infrastructure adapters)
- Domain fixture files (`*.domain-fixture.*`) must live under `plugins/` or `tests/`, not at repo root

This also runs automatically as a pre-commit hook.

### 2. Lint

```bash
ruff check .
```

Config in `ruff.toml`. Line length is 100 characters. The pre-commit hook runs `ruff check --fix` (auto-fixes safe issues).

### 3. Format

```bash
ruff format --check .
```

To apply formatting: `ruff format .`. The pre-commit hook auto-formats on commit.

### 4. Type check

```bash
pyright
```

### 5. Tests

```bash
uv run pytest                              # Full suite
uv run pytest core/domain/tests/           # Single layer
uv run pytest -k test_boss_initialization  # Single test by name
uv run pytest -m integration               # By marker (requires running test-db)
```

Tests use async auto-mode and follow Given-When-Then style with `# Given:` / `# When:` / `# Then:` comments.

### Full Quality Check Sequence

```bash
python3 scripts/check_architecture_boundaries.py
ruff check .
ruff format --check .
pyright
uv run pytest
```

### Pre-commit Hook Summary

Defined in `.pre-commit-config.yaml`. These run automatically on `git commit`:

| Order | Hook | Source | Action |
|-------|------|--------|--------|
| 1 | `check-architecture-boundaries` | Local (`scripts/check_architecture_boundaries.py`) | Rejects if `core/` imports `infrastructure/`, OpenHands SDK leaks into `core/`, or domain fixtures at repo root |
| 2 | `ruff` | `astral-sh/ruff-pre-commit` v0.14.5 | Lint with auto-fix (`--fix`) |
| 3 | `ruff-format` | `astral-sh/ruff-pre-commit` v0.14.5 | Auto-format code |
| 4 | `trailing-whitespace` | `pre-commit-hooks` v5.0.0 | Strip trailing whitespace |
| 5 | `end-of-file-fixer` | `pre-commit-hooks` v5.0.0 | Ensure files end with newline |
| 6 | `check-yaml` | `pre-commit-hooks` v5.0.0 | Validate YAML syntax |
| 7 | `check-added-large-files` | `pre-commit-hooks` v5.0.0 | Block large file commits |
| 8 | `check-merge-conflict` | `pre-commit-hooks` v5.0.0 | Detect merge conflict markers |
| 9 | `check-toml` | `pre-commit-hooks` v5.0.0 | Validate TOML syntax |
| 10 | `mixed-line-ending` | `pre-commit-hooks` v5.0.0 | Normalize line endings |

Run all hooks manually against all files:

```bash
uv run pre-commit run --all-files
```

## Configuration Hierarchy

Config loads with increasing priority (later overrides earlier):

1. `config/config.yaml` -- base defaults
2. `config/config.{ARISE_ENV}.yaml` -- environment overlay
3. Environment variables -- highest priority

Available overlays:

| `ARISE_ENV` | Overlay File | Key Differences |
|-------------|-------------|-----------------|
| `development` (default) | `config/config.development.yaml` | `log_level: DEBUG`, `verbose: true` |
| `dev` | `config/config.dev.yaml` | Supabase DB host on port 6543, cost-effective model |
| `production` | `config/config.production.yaml` | `log_level: WARNING`, `verbose: false`, restricted CORS origins |

Environment variables use the `ARISE_` prefix and nested keys use `_` separators. Example: `ARISE_INFRA_POSTGRES_HOST=db` overrides `database.host`. Config loading logic is in `config/settings.py`.

## Branch Strategy

The main branch is **`develop`**. There is no `main` or `master` branch. All PRs target `develop`.

No CI/CD is configured. All quality checks (architecture boundaries, lint, format, typecheck, tests) must be run locally before pushing. Pre-commit hooks provide a safety net for lint and formatting on commit.

### Contribution Flow

1. Create a feature branch from `develop`:
   ```bash
   git checkout develop && git pull
   git checkout -b feature/your-feature-name
   ```

2. Make changes. Run quality checks before committing:
   ```bash
   python3 scripts/check_architecture_boundaries.py
   ruff check .
   ruff format --check .
   pyright
   uv run pytest
   ```

3. Commit. Pre-commit hooks run automatically (architecture boundary check, ruff lint+format, etc.). If a hook fails, fix the issue and commit again.

4. Push and open a PR against `develop`:
   ```bash
   git push -u origin feature/your-feature-name
   gh pr create --base develop
   ```

## Release Process

No formal release process is defined. The `prod` profile (`docker compose --profile prod up -d`) builds the `production` Dockerfile stage with `--no-dev` dependencies and `restart: unless-stopped`.

## SEC-bench Tooling

SEC-bench tasks run CVE reproduction and patching inside Docker containers using a Docker-out-of-Docker (DooD) pattern.

### Building tool-enriched images

```bash
deployment/build-secbench-tools.sh deployment/njs-cve-2022-28049.json
```

This script:
1. Reads the CVE JSON to extract the base Docker image name via `CVEInstance.from_json_file()`.
2. Builds a new image layered on top with security analysis tools. Valgrind is always installed; KLEE installation is attempted but falls back gracefully if unavailable in the base image's package repos.
3. Uses `deployment/secbench-tools.Dockerfile` as the build template.
4. Skips images that already exist locally.

You can also pass a raw base image name:

```bash
deployment/build-secbench-tools.sh hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838
```

Multiple inputs can be passed in a single invocation.

### CVE JSON files

Sample CVE descriptors live in `deployment/`:

- `deployment/faad2-cve-2021-32272.json`
- `deployment/gpac-cve-2023-5586.json`
- `deployment/imagemagick-cve-2019-13309.json`
- `deployment/mruby-cve-2022-0240.json`
- `deployment/njs-cve-2022-28049.json`
- `deployment/njs-cve-2022-32414.json`
- `deployment/openjpeg-cve-2016-7445.json`
- `deployment/test_instance.json`

Each JSON defines a `CVEInstance` (target image, vulnerability details) consumed by the security plugin in `plugins/security/`.

### Docker-out-of-Docker

The `app` container mounts `/var/run/docker.sock` so it can launch sibling containers for SEC-bench workers. Set `HOST_PROJECT_ROOT` to the absolute host path of the repo for correct volume mapping when workers write output artifacts to `output/`.

## Database

PostgreSQL 16 with event sourcing. The schema is a single `events` table defined in `infrastructure/sql/create_events_table.sql`. It is auto-applied on first DB startup via Docker's `docker-entrypoint-initdb.d` mechanism.

The table uses optimistic concurrency control via a unique constraint on `(aggregate_id, sequence_number)`.

### Reset local database

```bash
docker compose --profile local down -v   # removes the postgres_data volume
docker compose --profile local up -d     # recreates with fresh schema
```

The test profile (`test-db`) uses `tmpfs` storage, so data is automatically discarded when the container stops.

### Common Docker Commands

```bash
# Build/rebuild images
docker compose --profile local build

# Shell into the app container
docker exec -it arise-app bash

# Run a task inside the app container
docker compose --profile local exec app python main.py run "your task"

# View logs
docker compose --profile local logs -f app

# Tear down (preserves data volume)
docker compose --profile local down

# Tear down and destroy data
docker compose --profile local down -v
```
