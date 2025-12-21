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

## Utility Scripts

### ReactFlow Export (`scripts/generate_reactflow.py`)

Generates a standalone ReactFlow component from an agent hierarchy. Useful for embedding agent trees in documentation or external applications.

**Features:**
- Fetches hierarchy from the REST API
- Color-codes nodes by role:
  - **BOSS**: purple (`#7c3aed`)
  - **MANAGER**: blue (`#2563eb`)
  - **WORKER**: green (`#16a34a`)
  - **PENDING**: yellow (`#eab308`)
- **Click on any node** to view detailed agent information in a modal:
  - **Objective**: Full task description
  - **Complexity Evaluation**: Complexity rating (simple/complex) with reasoning
  - **Configuration**: Strategy, worker tool, and config details
  - **Subtasks**: List of subtasks with status (for manager agents)
  - **Result/Error**: Completion result or error message if applicable
- Calculates tree layout positions automatically
- Outputs valid JSX ready to use in any React project

**Usage:**

```bash
# List all available agents
python3 scripts/generate_reactflow.py --list

# Generate component for the latest agent (output to stdout)
python3 scripts/generate_reactflow.py > AgentTree.jsx

# Generate for a specific agent by ID
python3 scripts/generate_reactflow.py 12dad0e4-f455-4fc0-92e2-0c04644a92e1 > AgentTree.jsx

# Generate for the 3rd most recent agent (0-indexed)
python3 scripts/generate_reactflow.py -n 2 > AgentTree.jsx
```

**Requirements:**
- API server running at `http://localhost:8000`
- `curl` available in PATH
- Python 3.x (no external dependencies)

**Output Example:**

```jsx
import React, { useCallback } from 'react';
import ReactFlow, { Background, Controls, MiniMap, ... } from 'reactflow';

const initialNodes = [
  { id: '...', position: { x: 0, y: 0 }, data: { label: 'BOSS (waiting)\n...' }, style: { background: '#7c3aed', ... } },
  // ...more nodes
];

const initialEdges = [
  { id: 'e-...', source: '...', target: '...', type: 'smoothstep' },
  // ...more edges
];

export default function ReactFlowTree() { ... }
```
