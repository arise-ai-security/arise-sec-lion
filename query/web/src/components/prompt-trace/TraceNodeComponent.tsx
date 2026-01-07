/**
 * Custom React Flow node for displaying agent trace information.
 *
 * Shows role badge, task summary, and prompt count indicator.
 * Clicking the node selects it for detail view.
 */

import { Handle, Position } from '@xyflow/react';

export interface TraceNodeData {
  /** Agent UUID. */
  agent_id: string;
  /** Agent role (boss, manager, worker, pending). */
  role: string;
  /** Task description. */
  task: string;
  /** Number of prompts sent by this agent. */
  prompt_count: number;
  /** Depth in hierarchy. */
  depth: number;
  /** Whether this node is selected. */
  selected?: boolean;
  /** Required for React Flow compatibility. */
  [key: string]: unknown;
}

interface TraceNodeComponentProps {
  data: TraceNodeData;
}

const roleColors: Record<string, string> = {
  boss: 'bg-purple-600 border-purple-700',
  BOSS: 'bg-purple-600 border-purple-700',
  manager: 'bg-blue-600 border-blue-700',
  MANAGER: 'bg-blue-600 border-blue-700',
  worker: 'bg-green-600 border-green-700',
  WORKER: 'bg-green-600 border-green-700',
  pending: 'bg-yellow-500 border-yellow-600',
  PENDING: 'bg-yellow-500 border-yellow-600',
};

export function TraceNodeComponent({ data }: TraceNodeComponentProps) {
  const colorClass = roleColors[data.role] ?? 'bg-gray-500 border-gray-600';
  const roleLabel = data.role.toUpperCase();

  return (
    <div
      className={`
        px-4 py-3 min-w-48 max-w-64
        ${colorClass} border-2 rounded-lg
        text-white shadow-lg
        ${data.selected ? 'ring-4 ring-white ring-opacity-50' : ''}
        cursor-pointer hover:opacity-90 transition-opacity
      `}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="w-3 h-3 bg-gray-300 border-2 border-gray-400"
      />

      {/* Header with role and prompt count */}
      <div className="flex items-center justify-between mb-2">
        <span className="font-bold text-sm">{roleLabel}</span>
        <span
          className="bg-white/20 px-2 py-0.5 rounded text-xs"
          title={`${data.prompt_count} prompt${data.prompt_count !== 1 ? 's' : ''}`}
        >
          {data.prompt_count} prompts
        </span>
      </div>

      {/* Task description */}
      <p className="text-xs opacity-90 line-clamp-2">
        {data.task || 'No task'}
      </p>

      {/* Depth indicator */}
      <div className="mt-2 text-xs opacity-70">
        Depth: {data.depth}
      </div>

      <Handle
        type="source"
        position={Position.Bottom}
        className="w-3 h-3 bg-gray-300 border-2 border-gray-400"
      />
    </div>
  );
}
