# API Reference

This document describes the REST API and SSE streaming endpoints.

---

## Base URL

- Local: `http://localhost:8000/api`
- Docs: `http://localhost:8000/api/docs` (Swagger UI)

---

## Agent Endpoints

### List Boss Agents

```
GET /api/agents?limit=10&offset=0
```

Returns paginated list of root BOSS agents.

**Response:**
```json
{
  "items": [
    {
      "id": "uuid",
      "role": "BOSS",
      "status": "completed",
      "task_description": "Create fizzbuzz",
      "created_at": "2024-01-15T10:30:00Z"
    }
  ],
  "pagination": {
    "limit": 10,
    "offset": 0,
    "total": 42,
    "has_more": true
  }
}
```

### Get Agent

```
GET /api/agents/{agent_id}
```

### Get Agent Hierarchy

```
GET /api/agents/{agent_id}/hierarchy
```

Returns tree structure of agent and all descendants.

**Response:**
```json
{
  "root": {
    "id": "uuid",
    "role": "BOSS",
    "status": "completed",
    "task_description": "...",
    "parent_id": null,
    "children": [
      {
        "id": "child-uuid",
        "role": "WORKER",
        "children": []
      }
    ]
  },
  "total_agents": 5,
  "depth": 2
}
```

### Get Agent Summary

```
GET /api/agents/{agent_id}/summary
```

Returns CQRS projection with computed summary.

### Get Execution Summary

```
GET /api/agents/{agent_id}/execution-summary
```

Returns cost breakdown, timing, and node counts.

---

## Event Endpoints

### Get Agent Events (Categorized)

```
GET /api/events/{agent_id}
```

**Response:**
```json
{
  "received": [...],
  "produced": [...],
  "passed": [...],
  "thinking": [...]
}
```

### Get All Events

```
GET /api/events/{agent_id}/all
```

Returns flat list of all events for agent.

### Get Hierarchy Events

```
GET /api/events/hierarchy/{root_id}/all
```

Returns all events for entire agent hierarchy.

### SSE Stream

```
GET /api/events/sse/{root_id}
```

Server-Sent Events stream for real-time updates.

**Event types:**
- `event` - New domain event
- `summary` - Updated execution summary

**Usage:**
```typescript
const source = new EventSource('/api/events/sse/' + rootId);
source.addEventListener('event', (e) => {
  const event = JSON.parse(e.data);
  console.log(event.event_type);
});
```

---

## Prompt Endpoints

### List All Prompts

```
GET /api/prompts
```

### Get Prompt

```
GET /api/prompts/{category}/{name}
```

### Update Prompt

```
PUT /api/prompts/{category}/{name}
Content-Type: application/json

{"content": "New prompt content"}
```

### Preview Prompt

```
POST /api/prompts/{category}/{name}/preview
```

Renders prompt with current variables.

### Reset Prompt

```
POST /api/prompts/{category}/{name}/reset
```

Resets to default template.

---

## Config Endpoint

### Get System Config

```
GET /api/config
```

Returns current system configuration.

---

## Health Check

```
GET /api/health
```

**Response:**
```json
{"status": "healthy"}
```

---

## TypeScript Types

Types are defined in `query/web/src/types/api.ts`:

```typescript
// Agent types
type AgentRole = 'BOSS' | 'MANAGER' | 'WORKER' | 'PENDING';
type AgentStatus = 'pending' | 'analyzing' | 'in_progress' |
                   'waiting' | 'completed' | 'failed';

interface AgentListItem {
  id: string;
  role: AgentRole;
  status: AgentStatus;
  task_description: string;
  created_at: string | null;
}

// Event types
interface DomainEvent {
  event_type: string;
  aggregate_id: string;
  sequence_number: number;
  occurred_at: string;
  data: Record<string, unknown>;
}
```

---

## API Client

Client functions in `query/web/src/api/client.ts`:

```typescript
import { listBossAgents, getAgentHierarchy, createEventSource } from './api/client';

// Fetch agents
const agents = await listBossAgents();

// Get hierarchy
const hierarchy = await getAgentHierarchy(agentId);

// Subscribe to SSE
const source = createEventSource(rootId);
source.onmessage = (e) => console.log(JSON.parse(e.data));
```

---

## Error Responses

```json
{
  "detail": "Agent abc123 not found"
}
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request (invalid parameters) |
| 404 | Resource not found |
| 500 | Internal server error |
