/**
 * Custom React Flow node for displaying agent information.
 */

import { Handle, Position } from '@xyflow/react';
import type { AgentRole, AgentStatus } from '../types/api';

export interface AgentNodeData {
  role: AgentRole;
  status: AgentStatus;
  task_description: string;
  restart_count?: number;
  was_restarted?: boolean;
  [key: string]: unknown; // Required for React Flow compatibility
}

interface AgentNodeComponentProps {
  data: AgentNodeData;
}

const roleColors: Record<AgentRole, string> = {
  BOSS: 'bg-purple-600 border-purple-700',
  MANAGER: 'bg-blue-600 border-blue-700',
  WORKER: 'bg-green-600 border-green-700',
  PENDING: 'bg-yellow-500 border-yellow-600',
};

const statusIcons: Record<AgentStatus, string> = {
  pending: '⏳',
  analyzing: '🔍',
  in_progress: '🔄',
  waiting: '⏸️',
  completed: '✅',
  failed: '❌',
  blocked: '🚫',
};

export function AgentNodeComponent({ data }: AgentNodeComponentProps) {
  // API returns lowercase roles, normalize to uppercase for lookup
  const normalizedRole = data.role.toUpperCase() as AgentRole;
  const colorClass = roleColors[normalizedRole] || 'bg-gray-500 border-gray-600';
  const statusIcon = statusIcons[data.status] || '❓';
  const restartCount = data.restart_count ?? 0;
  const wasRestarted = data.was_restarted ?? restartCount > 0;

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
        <span className="font-bold text-sm">{normalizedRole}</span>
        <span className="text-lg" title={data.status}>
          {statusIcon}
        </span>
      </div>

      <p className="text-xs opacity-90 line-clamp-2">{data.task_description || 'No task'}</p>
      {wasRestarted && (
        <div className="mt-2">
          <span
            className="inline-flex items-center rounded-full border border-amber-200 bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-900"
            title={`Restarted ${restartCount} time${restartCount === 1 ? '' : 's'}`}
          >
            RESTARTED ×{restartCount}
          </span>
        </div>
      )}

      <Handle
        type="source"
        position={Position.Bottom}
        className="w-3 h-3 bg-gray-300 border-2 border-gray-400"
      />
    </div>
  );
}
