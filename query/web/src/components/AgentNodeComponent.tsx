/**
 * Custom React Flow node for displaying agent information.
 */

import { Handle, Position } from '@xyflow/react';
import type { AgentRole, AgentStatus } from '../types/api';

export interface AgentNodeData {
  role: AgentRole;
  status: AgentStatus;
  task_description: string;
  [key: string]: unknown; // Required for React Flow compatibility
}

interface AgentNodeComponentProps {
  data: AgentNodeData;
}

const roleColors: Record<Lowercase<AgentRole>, string> = {
  boss: 'bg-purple-600 border-purple-700',
  manager: 'bg-blue-600 border-blue-700',
  worker: 'bg-green-600 border-green-700',
  pending: 'bg-yellow-500 border-yellow-600',
};

const statusIcons: Record<AgentStatus, string> = {
  pending: '⏳',
  analyzing: '🔍',
  in_progress: '🔄',
  waiting: '⏸️',
  completed: '✅',
  failed: '❌',
  blocked: '🚫',
  terminated: '⛔',
  verifying: '🔬',
};

export function AgentNodeComponent({ data }: AgentNodeComponentProps) {
  const roleKey = data.role.toLowerCase() as Lowercase<AgentRole>;
  const colorClass = roleColors[roleKey] || 'bg-gray-500 border-gray-600';
  const statusIcon = statusIcons[data.status] || '❓';

  return (
    <div
      className={`
        px-4 py-3 min-w-48 max-w-64
        ${colorClass} border-2 rounded-lg
        text-white shadow-lg
      `}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="w-3 h-3 bg-gray-300 border-2 border-gray-400"
      />

      <div className="flex items-center justify-between mb-2">
        <span className="font-bold text-sm">{data.role}</span>
        <span className="text-lg" title={data.status}>
          {statusIcon}
        </span>
      </div>

      <p className="text-xs opacity-90 line-clamp-2">{data.task_description || 'No task'}</p>

      <Handle
        type="source"
        position={Position.Bottom}
        className="w-3 h-3 bg-gray-300 border-2 border-gray-400"
      />
    </div>
  );
}
