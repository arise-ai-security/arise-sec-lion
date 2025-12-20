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
| `AgentNodeComponent` | `src/components/AgentNodeComponent.tsx` | Custom node with role colors and status icons |
| `SummaryPanel` | `src/components/SummaryPanel.tsx` | Agent summary (task, complexity, config) |
| `EventPanel` | `src/components/EventPanel.tsx` | Categorized event display with filtering |
| `CostPanel` | `src/components/CostPanel.tsx` | Real-time cost breakdown and metrics |
| `ConfigPanel` | `src/components/ConfigPanel.tsx` | System configuration modal |

## API Client

Located at `src/api/client.ts`:

```typescript
// Agent queries
listBossAgents(): Promise<AgentListItem[]>
getAgentHierarchy(agentId: string): Promise<AgentHierarchy>
getAgentSummary(agentId: string): Promise<AgentSummary>
getExecutionSummary(agentId: string): Promise<ExecutionSummary>

// Event queries
getAgentEvents(agentId: string): Promise<CategorizedEvents>
getAllAgentEvents(agentId: string): Promise<DomainEvent[]>

// SSE streaming
createEventSource(rootId: string): EventSource
createSummaryEventSource(rootId: string): EventSource

// System config
getSystemConfig(): Promise<SystemConfig>
```

## Hooks

| Hook | File | Purpose |
|------|------|---------|
| `useSSE` | `src/hooks/useSSE.ts` | Event stream connection with auto-reconnect |
| `useSummarySSE` | `src/hooks/useSummarySSE.ts` | Real-time cost/timing summary updates |

## Real-time Updates

The dashboard uses SSE for live updates via the `useSSE` hook:

- Connects to `/api/events/sse/{root_id}`
- Auto-refreshes hierarchy on structure-changing events
- Limits event buffer to 100 entries
- Shows connection status indicator

## Project Structure

```
query/web/
├── src/
│   ├── App.tsx                    # Main application
│   ├── api/client.ts              # API client functions
│   ├── components/
│   │   ├── AgentSidebar.tsx       # BOSS agent selector
│   │   ├── AgentTree.tsx          # XYFlow hierarchy
│   │   ├── AgentNodeComponent.tsx # Custom tree node
│   │   ├── SummaryPanel.tsx       # Agent summary view
│   │   ├── EventPanel.tsx         # Event display
│   │   ├── CostPanel.tsx          # Cost breakdown
│   │   └── ConfigPanel.tsx        # Config modal
│   ├── hooks/
│   │   ├── useSSE.ts              # Event stream hook
│   │   └── useSummarySSE.ts       # Summary stream hook
│   └── types/api.ts               # TypeScript interfaces
├── package.json
└── vite.config.ts
```

## Docker Integration

The web dashboard is containerized with nginx for production:

- `Dockerfile` - Multi-stage build (node → nginx)
- `nginx.conf` - Serves static files, proxies `/api` to backend
- Deployed via `deployment/docker-compose.yaml` as `web` service
