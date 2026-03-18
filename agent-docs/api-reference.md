# API Reference

Base URL: `http://localhost:8000/api`
Swagger UI: `http://localhost:8000/api/docs`

## Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents?limit=10&offset=0` | List boss agents (paginated) |
| GET | `/api/agents/{agent_id}` | Get agent details |
| GET | `/api/agents/{agent_id}/hierarchy` | Agent tree with all descendants |
| GET | `/api/agents/{agent_id}/summary` | CQRS projection summary |
| GET | `/api/agents/{agent_id}/execution-summary` | Cost, timing, node counts |

## Events

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events/{agent_id}` | Categorized events (received/produced/passed/thinking) |
| GET | `/api/events/{agent_id}/all` | Flat list of all events |
| GET | `/api/events/hierarchy/{root_id}/all` | All events for entire hierarchy |
| GET | `/api/events/sse/{root_id}` | SSE stream (event types: `event`, `summary`) |

## Prompts

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/prompts` | List all prompts |
| GET | `/api/prompts/{category}/{name}` | Get prompt |
| PUT | `/api/prompts/{category}/{name}` | Update prompt (`{"content": "..."}`) |
| POST | `/api/prompts/{category}/{name}/preview` | Render with current variables |
| POST | `/api/prompts/{category}/{name}/reset` | Reset to default |

## Prompt Traces

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/trace/{root_id}` | Parsed prompts with provenance-grouped sections |

## Other

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Current system configuration |
| GET | `/api/health` | Health check (`{"status": "healthy"}`) |

## SSE Usage

```typescript
const source = new EventSource('/api/events/sse/' + rootId);
source.addEventListener('event', (e) => {
  const event = JSON.parse(e.data);
});
```

## Error Responses

```json
{"detail": "Agent abc123 not found"}
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request |
| 404 | Not found |
| 500 | Internal error |
