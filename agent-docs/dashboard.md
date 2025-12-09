# Agent Dashboard Documentation

React-based dashboard for real-time visualization of agent hierarchy and events.

## Tech Stack

| Technology | Purpose |
|------------|---------|
| React 19 | UI framework |
| TypeScript | Type safety |
| Vite | Build tool and dev server |
| Tailwind CSS 4 | Styling |
| XYFlow (React Flow) | Tree visualization |

## Running the Dashboard

```bash
cd presentation/web

# Development
npm install
npm run dev        # http://localhost:5173

# Production build
npm run build      # Output in dist/
```

## Key Components

| Component | File | Purpose |
|-----------|------|---------|
| `App` | `src/App.tsx` | Main layout, state management, SSE connection |
| `AgentSidebar` | `src/components/AgentSidebar.tsx` | BOSS agent list selector |
| `AgentTree` | `src/components/AgentTree.tsx` | XYFlow hierarchy visualization |
| `EventPanel` | `src/components/EventPanel.tsx` | Categorized event display with filtering |

## API Client

Located at `src/api/client.ts`:

```typescript
// Agent queries
listBossAgents(): Promise<AgentListItem[]>
getAgentHierarchy(agentId: string): Promise<AgentHierarchy>

// Event queries
getAgentEvents(agentId: string): Promise<CategorizedEvents>
getAllAgentEvents(agentId: string): Promise<DomainEvent[]>

// SSE streaming
createEventSource(rootId: string): EventSource
```

## Real-time Updates

The dashboard uses SSE for live updates via the `useSSE` hook:

- Connects to `/api/events/sse/{root_id}`
- Auto-refreshes hierarchy on structure-changing events
- Limits event buffer to 100 entries
- Shows connection status indicator

## Project Structure

```
presentation/web/
├── src/
│   ├── App.tsx           # Main application
│   ├── api/client.ts     # API client functions
│   ├── components/       # React components
│   ├── hooks/useSSE.ts   # SSE connection hook
│   └── types/api.ts      # TypeScript interfaces
├── package.json
└── vite.config.ts
```

## Docker Integration

The web dashboard is containerized with nginx for production:

- `Dockerfile` - Multi-stage build (node → nginx)
- `nginx.conf` - Serves static files, proxies `/api` to backend
- Deployed via `deployment/docker-compose.yaml` as `web` service
