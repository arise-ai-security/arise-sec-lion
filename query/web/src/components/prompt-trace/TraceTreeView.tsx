/**
 * React Flow tree visualization for prompt trace hierarchy.
 *
 * Displays agents as nodes with prompt counts, allowing selection
 * for detailed view.
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

import { TraceNodeComponent, type TraceNodeData } from './TraceNodeComponent';
import type { TraceAgentNode } from '../../types/api';

interface TraceTreeViewProps {
  /** Root node of the trace hierarchy. */
  root: TraceAgentNode;
  /** Currently selected agent ID. */
  selectedAgentId?: string;
  /** Callback when a node is clicked. */
  onNodeClick?: (agentId: string) => void;
}

type TraceFlowNode = Node<TraceNodeData>;

const nodeTypes = {
  trace: TraceNodeComponent,
};

const NODE_WIDTH = 200;
const NODE_HEIGHT = 100;
const HORIZONTAL_SPACING = 50;
const VERTICAL_SPACING = 120;

function buildNodesAndEdges(
  root: TraceAgentNode,
  selectedAgentId?: string
): { nodes: TraceFlowNode[]; edges: Edge[] } {
  const nodes: TraceFlowNode[] = [];
  const edges: Edge[] = [];

  // Calculate positions using a simple tree layout
  function layoutTree(node: TraceAgentNode, depth: number, offset: number): number {
    // If no children, place at offset
    if (node.children.length === 0) {
      nodes.push({
        id: node.agent_id,
        type: 'trace',
        position: {
          x: offset * (NODE_WIDTH + HORIZONTAL_SPACING),
          y: depth * (NODE_HEIGHT + VERTICAL_SPACING),
        },
        data: {
          agent_id: node.agent_id,
          role: node.role,
          task: node.task,
          prompt_count: node.prompt_count,
          depth: node.depth,
          selected: node.agent_id === selectedAgentId,
        },
      });
      return 1;
    }

    // Layout children first
    let childrenWidth = 0;
    for (const child of node.children) {
      edges.push({
        id: `${node.agent_id}-${child.agent_id}`,
        source: node.agent_id,
        target: child.agent_id,
        type: 'smoothstep',
        style: { stroke: '#94a3b8', strokeWidth: 2 },
      });
      childrenWidth += layoutTree(child, depth + 1, offset + childrenWidth);
    }

    // Place parent centered above children
    const parentX = offset + (childrenWidth - 1) / 2;
    nodes.push({
      id: node.agent_id,
      type: 'trace',
      position: {
        x: parentX * (NODE_WIDTH + HORIZONTAL_SPACING),
        y: depth * (NODE_HEIGHT + VERTICAL_SPACING),
      },
      data: {
        agent_id: node.agent_id,
        role: node.role,
        task: node.task,
        prompt_count: node.prompt_count,
        depth: node.depth,
        selected: node.agent_id === selectedAgentId,
      },
    });

    return childrenWidth;
  }

  layoutTree(root, 0, 0);
  return { nodes, edges };
}

export function TraceTreeView({
  root,
  selectedAgentId,
  onNodeClick,
}: TraceTreeViewProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<TraceFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    const { nodes: newNodes, edges: newEdges } = buildNodesAndEdges(root, selectedAgentId);
    setNodes(newNodes);
    setEdges(newEdges);
  }, [root, selectedAgentId, setNodes, setEdges]);

  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: TraceFlowNode) => {
      onNodeClick?.(node.id);
    },
    [onNodeClick],
  );

  const minimapNodeColor = useMemo(
    () => (node: TraceFlowNode) => {
      const role = node.data?.role?.toUpperCase();
      switch (role) {
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
