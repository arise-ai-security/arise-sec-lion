"""
Fetch agent hierarchy and generate a ReactFlow React component.

Nodes are color-coded by role:
  - BOSS: purple (#7c3aed)
  - MANAGER: blue (#2563eb)
  - WORKER: green (#16a34a)
  - PENDING: yellow (#eab308)

Usage:
  python3 scripts/generate_reactflow.py              # Use latest agent
  python3 scripts/generate_reactflow.py --list       # List all available agents
  python3 scripts/generate_reactflow.py <agent-id>   # Use specific agent ID
  python3 scripts/generate_reactflow.py -n 2         # Use the 3rd most recent agent
"""

import argparse
import json
import subprocess
import sys

API_BASE = "http://localhost:8000/api"

NODE_WIDTH = 200
HORIZONTAL_SPACING = 50
NODE_HEIGHT = 80
VERTICAL_SPACING = 100

# Color mapping by role
ROLE_COLORS = {
    "boss": "#7c3aed",  # purple
    "manager": "#2563eb",  # blue
    "worker": "#16a34a",  # green
    "pending": "#eab308",  # yellow
}


def curl_get(url: str) -> dict | list:
    """Execute curl and return JSON response."""
    result = subprocess.run(["curl", "-s", url], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching {url}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def list_agents() -> list:
    """Fetch and return list of all agents."""
    return curl_get(f"{API_BASE}/agents")


def get_hierarchy(agent_id: str) -> dict:
    """Fetch the hierarchy for a given agent ID."""
    return curl_get(f"{API_BASE}/agents/{agent_id}/hierarchy")


def truncate_label(text: str, max_len: int = 35) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def layout_tree(
    node: dict, depth: int, offset: int, nodes: list, edges: list
) -> int:
    """
    Recursively layout tree nodes.
    Returns width (number of leaf slots used).
    """
    children = node.get("children", [])
    role = node["role"]
    status = node["status"]
    color = ROLE_COLORS.get(role, "#6b7280")  # gray fallback

    # Escape single quotes in task description for JS string
    task_desc = node["task_description"].replace("'", "\\'").replace("\n", " ")
    label = f"{role.upper()} ({status})\n{truncate_label(task_desc)}"

    if not children:
        # Leaf node - place at current offset
        nodes.append(
            {
                "id": node["id"],
                "position": {
                    "x": offset * (NODE_WIDTH + HORIZONTAL_SPACING),
                    "y": depth * (NODE_HEIGHT + VERTICAL_SPACING),
                },
                "data": {"label": label},
                "style": {
                    "background": color,
                    "color": "white",
                    "border": "none",
                    "borderRadius": "8px",
                    "padding": "10px",
                    "fontSize": "11px",
                    "width": NODE_WIDTH,
                },
            }
        )
        return 1

    # Layout children first
    children_width = 0
    for child in children:
        edge_id = f"e-{node['id'][:8]}-{child['id'][:8]}"
        edges.append(
            {
                "id": edge_id,
                "source": node["id"],
                "target": child["id"],
                "type": "smoothstep",
            }
        )
        children_width += layout_tree(
            child, depth + 1, offset + children_width, nodes, edges
        )

    # Place parent centered above children
    parent_x = offset + (children_width - 1) / 2
    nodes.append(
        {
            "id": node["id"],
            "position": {
                "x": parent_x * (NODE_WIDTH + HORIZONTAL_SPACING),
                "y": depth * (NODE_HEIGHT + VERTICAL_SPACING),
            },
            "data": {"label": label},
            "style": {
                "background": color,
                "color": "white",
                "border": "none",
                "borderRadius": "8px",
                "padding": "10px",
                "fontSize": "11px",
                "width": NODE_WIDTH,
            },
        }
    )

    return children_width


def generate_react_component(nodes: list, edges: list) -> str:
    """Generate the ReactFlow component code."""
    # Format nodes as JSON with indentation
    nodes_json = json.dumps(nodes, indent=2)
    edges_json = json.dumps(edges, indent=2)

    # Build component using string concatenation to avoid f-string brace issues
    component = """import React, { useCallback } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
} from 'reactflow';
import 'reactflow/dist/style.css';

// Color legend:
// BOSS: purple (#7c3aed)
// MANAGER: blue (#2563eb)
// WORKER: green (#16a34a)
// PENDING: yellow (#eab308)

const initialNodes = """
    component += nodes_json
    component += """;

const initialEdges = """
    component += edges_json
    component += """;

export default function ReactFlowTree() {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge(params, eds)),
    [setEdges]
  );

  // MiniMap node color based on style background
  const minimapNodeColor = (node) => node.style?.background || '#6b7280';

  return (
    <div style={{ height: '100vh', width: '100%' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
      >
        <MiniMap nodeColor={minimapNodeColor} zoomable pannable />
        <Controls />
        <Background variant="dots" gap={12} size={1} />
      </ReactFlow>
    </div>
  );
}
"""
    return component


def print_agents_table(agents: list):
    """Print a formatted table of agents."""
    print("\nAvailable Agents:", file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    print(
        f"{'#':<4} {'ID':<38} {'Role':<8} {'Status':<12} {'Task Description':<35}",
        file=sys.stderr,
    )
    print("-" * 100, file=sys.stderr)

    for i, agent in enumerate(agents):
        task = agent["task_description"]
        if len(task) > 35:
            task = task[:32] + "..."
        print(
            f"{i:<4} {agent['id']:<38} {agent['role']:<8} {agent['status']:<12} {task:<35}",
            file=sys.stderr,
        )

    print("-" * 100, file=sys.stderr)
    print(f"Total: {len(agents)} agents\n", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Generate ReactFlow component from agent hierarchy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/generate_reactflow.py              # Use latest agent
  python3 scripts/generate_reactflow.py --list       # List all available agents
  python3 scripts/generate_reactflow.py <agent-id>   # Use specific agent ID
  python3 scripts/generate_reactflow.py -n 2         # Use the 3rd most recent agent
        """,
    )
    parser.add_argument("agent_id", nargs="?", help="Specific agent ID to use")
    parser.add_argument(
        "--list", "-l", action="store_true", help="List all available agents"
    )
    parser.add_argument(
        "-n", type=int, default=0, help="Use nth agent from list (0=latest, default)"
    )

    args = parser.parse_args()

    # Fetch agents list
    print("Fetching agents list...", file=sys.stderr)
    agents = list_agents()

    if not agents:
        print("No agents found!", file=sys.stderr)
        sys.exit(1)

    # List mode - just print agents and exit
    if args.list:
        print_agents_table(agents)
        sys.exit(0)

    # Determine which agent ID to use
    if args.agent_id:
        agent_id = args.agent_id
        # Validate agent ID exists
        agent_ids = [a["id"] for a in agents]
        if agent_id not in agent_ids:
            print(f"Error: Agent ID '{agent_id}' not found!", file=sys.stderr)
            print("Use --list to see available agents.", file=sys.stderr)
            sys.exit(1)
    else:
        # Use nth agent (default 0 = latest)
        if args.n >= len(agents):
            print(
                f"Error: Index {args.n} out of range. Only {len(agents)} agents available.",
                file=sys.stderr,
            )
            sys.exit(1)
        agent_id = agents[args.n]["id"]
        print(f"Using agent #{args.n}: {agent_id}", file=sys.stderr)

    # Fetch hierarchy
    print(f"Fetching hierarchy for {agent_id}...", file=sys.stderr)
    hierarchy = get_hierarchy(agent_id)

    root = hierarchy["root"]
    total_agents = hierarchy.get("total_agents", "?")
    depth = hierarchy.get("depth", "?")
    print(f"Hierarchy: {total_agents} agents, {depth} levels deep", file=sys.stderr)

    nodes = []
    edges = []
    layout_tree(root, 0, 0, nodes, edges)

    print(f"Generated {len(nodes)} nodes and {len(edges)} edges", file=sys.stderr)

    # Output the React component to stdout
    print(generate_react_component(nodes, edges))


if __name__ == "__main__":
    main()
