# Deployment

All commands run inside Docker containers via compose profiles.

**ALWAYS use `--profile local`** unless explicitly told otherwise.
- `local` — local PostgreSQL database (safe default)
- `dev` — external Supabase database
- `prod` — production
- `test` — test database only

## Commands

```bash
cd deployment

# Start services
docker compose --profile local up -d --build

# Run a task
docker compose --profile local exec app python main.py run "Your task"

# Run with worker configuration overrides
docker compose --profile local exec app python main.py run "Your task" \
  --worker-model gpt-4o \
  --worker-tool claude_code

# Run tests
docker compose --profile local exec app uv run pytest

# View results
docker compose --profile local exec app python main.py list
docker compose --profile local exec app python main.py events
docker compose --profile local exec app python main.py summary
```
