# REST API Documentation

The FastAPI-based REST API provides endpoints for querying agent state and streaming real-time events.

## Running the API Server

```bash
cd deployment
docker compose up --build -d

# API available at http://localhost:8000
# OpenAPI docs at http://localhost:8000/api/docs
```

## API Routes

### Agent Endpoints

| Route | Method | Description |
|-------|--------|-------------|
| `/api/agents` | GET | List all BOSS (root) agents |
| `/api/agents/{id}` | GET | Get single agent details |
| `/api/agents/{id}/hierarchy` | GET | Get agent hierarchy tree |
| `/api/agents/{id}/summary` | GET | Get CQRS projection summary for agent |
| `/api/agents/{id}/execution-summary` | GET | Get comprehensive execution metrics (cost, timing, node counts) |

### Event Endpoints

| Route | Method | Description |
|-------|--------|-------------|
| `/api/events/{id}` | GET | Get categorized events for agent |
| `/api/events/{id}/all` | GET | Get all events (chronological) |
| `/api/events/hierarchy/{root_id}/all` | GET | Get all events for entire hierarchy |
| `/api/events/sse/{root_id}` | GET | SSE stream for real-time event updates |
| `/api/events/sse/{root_id}/summary` | GET | SSE stream for real-time execution summary |

### System Endpoints

| Route | Method | Description |
|-------|--------|-------------|
| `/api/prompts` | GET | List all prompt templates |
| `/api/prompts/{category}/{name}` | GET/PUT | Get or update prompt |
| `/api/config` | GET | Get system configuration (read-only) |
| `/api/health` | GET | Health check endpoint |

## Key Files

| File | Purpose |
|------|---------|
| `query/api/app.py` | FastAPI app factory with lifespan management |
| `query/api/schemas.py` | Pydantic request/response schemas |
| `query/api/routes/agents.py` | Agent hierarchy and summary endpoints |
| `query/api/routes/events.py` | Event query and SSE streaming |
| `query/api/routes/prompts.py` | Prompt template CRUD |
| `query/api/routes/config.py` | System configuration endpoint |
| `query/api/dependencies.py` | FastAPI dependency injection |

## Event Categories

Events are categorized for the dashboard:

| Category | Events |
|----------|--------|
| **received** | `TaskAssigned` |
| **produced** | `AgentCreated`, `StatusChanged`, `ComplexityEvaluated`, `SubtasksDefined`, `WorkCompleted`, `WorkFailed` |
| **passed** | `ChildSpawned`, `ChildCompleted` |
| **thinking** | `ThoughtCaptured` |

## SSE Streaming

Real-time event streaming uses Server-Sent Events:

```javascript
const source = new EventSource('/api/events/sse/{root_id}');
source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log(data);
};
```

## Agent Summary Projection

The `/api/agents/{id}/summary` endpoint returns a CQRS projection that aggregates:

| Field | Source Event | Description |
|-------|--------------|-------------|
| `complexity` | `ComplexityEvaluated` | Why agent became WORKER vs MANAGER |
| `complexity_reasoning` | `ComplexityEvaluated` | LLM's reasoning for the decision |
| `worker_tool` | `CodeGenerationStarted` | Tool used (claude_code/openhands) |
| `subtasks` | `SubtasksDefined` | List of subtasks with child status |
| `config_strategy` | `AgentCreated` | Config strategy (per_operation/heuristic/hybrid) |
| `config_details` | `AgentCreated` | LLM models, hyperparameters |

## Execution Summary Endpoint

The `/api/agents/{id}/execution-summary` endpoint returns comprehensive metrics:

```json
{
  "total_events": 150,
  "events_by_type": {"AgentCreated": 10, "TaskAssigned": 10, ...},
  "error_count": 0,
  "node_counts": {
    "BOSS": 1, "MANAGER": 3, "WORKER": 8, "PENDING": 0, "total": 12
  },
  "cost": {
    "total_cost_usd": 1.85,
    "llm_cost_usd": 1.50,
    "worker_cost_usd": 0.35,
    "cost_by_role": {"BOSS": 0.10, "MANAGER": 0.40, "WORKER": 1.35},
    "cost_by_model": {"gpt-4o": 0.80, "claude-sonnet": 1.05},
    "tokens_by_role": {"BOSS": 1000, "MANAGER": 4000, "WORKER": 12000}
  },
  "timing": {
    "total_seconds": 45.2,
    "by_role": {"BOSS": 5.0, "MANAGER": 15.0, "WORKER": 25.2},
    "by_phase": {"analyzing": 10.0, "in_progress": 35.2}
  }
}
```

## System Configuration

The `/api/config` endpoint returns read-only system settings:

- **Infrastructure**: BOSS model, worker tool type/model, timeouts
- **Application**: Retries, polling interval, complexity threshold

Note: Sensitive data (passwords, API keys) is never exposed.

## Architecture Notes

- **Lifespan management**: Database connection handled via FastAPI lifespan context
- **Event store injection**: Attached to `app.state` and injected via `Depends()`
- **CORS**: Configured for Vite dev server ports (5173, 3000)
- **Static files**: Production build served from root path when `static_dir` provided
