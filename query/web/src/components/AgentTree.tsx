/**
 * Agent hierarchy tree visualization using React Flow.
 */

import { useCallback, useEffect, useMemo } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { AgentNodeComponent, type AgentNodeData } from './AgentNodeComponent';
import type { AgentNode as AgentNodeType } from '../types/api';

interface AgentTreeProps {
  hierarchy: { root: AgentNodeType } | null;
  onNodeClick?: (agentId: string) => void;
  hasAgents?: boolean;
}

type AgentFlowNode = Node<AgentNodeData>;

const nodeTypes = {
  agent: AgentNodeComponent,
};

const NODE_WIDTH = 200;
const NODE_HEIGHT = 80;
const HORIZONTAL_SPACING = 50;
const VERTICAL_SPACING = 100;

function buildNodesAndEdges(root: AgentNodeType): { nodes: AgentFlowNode[]; edges: Edge[] } {
  const nodes: AgentFlowNode[] = [];
  const edges: Edge[] = [];

  // Calculate positions using a simple tree layout
  function layoutTree(node: AgentNodeType, depth: number, offset: number): number {
    // If no children, place at offset
    if (node.children.length === 0) {
      nodes.push({
        id: node.id,
        type: 'agent',
        position: {
          x: offset * (NODE_WIDTH + HORIZONTAL_SPACING),
          y: depth * (NODE_HEIGHT + VERTICAL_SPACING),
        },
        data: {
          role: node.role,
          status: node.status,
          task_description: node.task_description,
          restart_count: node.restart_count,
          was_restarted: node.was_restarted,
          hang_restart_count: node.hang_restart_count,
          was_hang_restarted: node.was_hang_restarted,
          idle_seconds: node.idle_seconds,
          is_stale: node.is_stale,
        },
      });
      return 1;
    }

    // Layout children first
    let childrenWidth = 0;
    for (const child of node.children) {
      edges.push({
        id: `${node.id}-${child.id}`,
        source: node.id,
        target: child.id,
        type: 'smoothstep',
      });
      childrenWidth += layoutTree(child, depth + 1, offset + childrenWidth);
    }

    // Place parent centered above children
    const parentX = offset + (childrenWidth - 1) / 2;
    nodes.push({
      id: node.id,
      type: 'agent',
      position: {
        x: parentX * (NODE_WIDTH + HORIZONTAL_SPACING),
        y: depth * (NODE_HEIGHT + VERTICAL_SPACING),
      },
      data: {
        role: node.role,
        status: node.status,
        task_description: node.task_description,
        restart_count: node.restart_count,
        was_restarted: node.was_restarted,
        hang_restart_count: node.hang_restart_count,
        was_hang_restarted: node.was_hang_restarted,
        idle_seconds: node.idle_seconds,
        is_stale: node.is_stale,
      },
    });

    return childrenWidth;
  }

  layoutTree(root, 0, 0);
  return { nodes, edges };
}

export function AgentTree({ hierarchy, onNodeClick, hasAgents = true }: AgentTreeProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<AgentFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    if (hierarchy?.root) {
      const { nodes: newNodes, edges: newEdges } = buildNodesAndEdges(hierarchy.root);
      setNodes(newNodes);
      setEdges(newEdges);
    }
  }, [hierarchy, setNodes, setEdges]);

  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: AgentFlowNode) => {
      onNodeClick?.(node.id);
    },
    [onNodeClick],
  );

  const minimapNodeColor = useMemo(
    () => (node: AgentFlowNode) => {
      switch (node.data?.role) {
        case 'BOSS':
          return '#7c3aed';
        case 'MANAGER':
          return '#2563eb';
        case 'WORKER':
          return '#16a34a';
        case 'PENDING':
          return '#eab308';
        default:
          return '#6b7280';
      }
    },
    [],
  );

  if (!hierarchy) {
    // Different messages based on whether we have any agents at all
    const message = hasAgents
      ? 'Select an agent to view hierarchy'
      : 'No agent runs yet. Start a task to see the hierarchy.';

    return (
      <div className="flex items-center justify-center h-full text-gray-500">
        {message}
      </div>
    );
  }

  return (
    <div className="w-full h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
      >
        <Background />
        <Controls />
        <MiniMap nodeColor={minimapNodeColor} />
      </ReactFlow>
    </div>
  );
}
