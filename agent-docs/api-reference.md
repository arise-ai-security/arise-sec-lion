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
| GET | `/api/events/{agent_id}/all` | Flat list of all events (paginated) |
| GET | `/api/events/{agent_id}/prompts` | Rendered prompts sent to this agent |
| GET | `/api/events/hierarchy/{root_id}/all` | All events for entire hierarchy (paginated) |
| GET | `/api/events/sse/{root_id}` | SSE stream of events (event type: `event`) |
| GET | `/api/events/sse/{root_id}/summary` | SSE stream of hierarchy summary snapshots (event type: `summary`) |

## Prompts

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/prompts` | List all prompts |
| GET | `/api/prompts/variables` | Available template variables |
| GET | `/api/prompts/{category}` | List prompts in a category |
| GET | `/api/prompts/{category}/{name}` | Get prompt |
| PUT | `/api/prompts/{category}/{name}` | Update prompt (`{"content": "..."}`) |
| POST | `/api/prompts/{category}/{name}/preview` | Render with current variables |
| POST | `/api/prompts/{category}/{name}/reset` | Reset to default |

## Prompt Traces

Router prefix: `/api/prompt-trace`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/prompt-trace/trace/{root_id}` | Hierarchy trace: parsed prompts with provenance-grouped sections |
| GET | `/api/prompt-trace/agent/{agent_id}` | Prompt trace for a single agent node |

## Other

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Current system configuration |
| GET | `/api/health` | Health check (`{"status": "healthy"}`) |

## SSE Usage

Per-event stream (`event` events) and the summary stream (`summary` events) are
served from two separate endpoints:

```typescript
const events = new EventSource('/api/events/sse/' + rootId);
events.addEventListener('event', (e) => {
  const event = JSON.parse(e.data);
});

const summary = new EventSource('/api/events/sse/' + rootId + '/summary');
summary.addEventListener('summary', (e) => {
  const snapshot = JSON.parse(e.data);
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
