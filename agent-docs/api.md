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

| Route | Method | Description |
|-------|--------|-------------|
| `/api/agents` | GET | List all BOSS (root) agents |
| `/api/agents/{id}` | GET | Get single agent details |
| `/api/agents/{id}/hierarchy` | GET | Get agent hierarchy tree |
| `/api/events/{id}` | GET | Get categorized events for agent |
| `/api/events/{id}/all` | GET | Get all events (chronological) |
| `/api/events/hierarchy/{root_id}/all` | GET | Get all events for entire hierarchy |
| `/api/events/sse/{root_id}` | GET | SSE stream for real-time updates |
| `/api/prompts` | GET | List all prompt templates |
| `/api/prompts/{category}/{name}` | GET/PUT | Get or update prompt |
| `/api/health` | GET | Health check endpoint |

## Key Files

| File | Purpose |
|------|---------|
| `presentation/api/app.py` | FastAPI app factory with lifespan management |
| `presentation/api/schemas.py` | Pydantic request/response schemas |
| `presentation/api/routes/agents.py` | Agent hierarchy endpoints |
| `presentation/api/routes/events.py` | Event query and SSE streaming |
| `presentation/api/routes/prompts.py` | Prompt template CRUD |
| `presentation/api/dependencies.py` | FastAPI dependency injection |

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

## Architecture Notes

- **Lifespan management**: Database connection handled via FastAPI lifespan context
- **Event store injection**: Attached to `app.state` and injected via `Depends()`
- **CORS**: Configured for Vite dev server ports (5173, 3000)
- **Static files**: Production build served from root path when `static_dir` provided
