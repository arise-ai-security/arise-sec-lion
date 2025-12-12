/**
 * Sidebar component showing list of BOSS agents.
 */

import type { AgentListItem } from '../types/api';

interface AgentSidebarProps {
  agents: AgentListItem[];
  selectedId: string | null;
  onSelect: (agentId: string) => void;
  loading: boolean;
}

const statusColors: Record<string, string> = {
  INITIALIZING: 'bg-yellow-400',
  WORKING: 'bg-blue-400',
  COMPLETED: 'bg-green-400',
  FAILED: 'bg-red-400',
};

export function AgentSidebar({ agents, selectedId, onSelect, loading }: AgentSidebarProps) {
  if (loading) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading agents...</div>
      </div>
    );
  }

  if (agents.length === 0) {
    return (
      <div className="p-4 text-gray-500">
        No agents found. Start a task to see agents here.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <h2 className="px-4 py-3 text-lg font-semibold text-gray-800 dark:text-gray-100 border-b dark:border-gray-700">
        Agent Runs
      </h2>
      <div className="flex-1 overflow-y-auto">
        {agents.map((agent) => (
          <button
            key={agent.id}
            onClick={() => onSelect(agent.id)}
            className={`
              w-full px-4 py-3 text-left border-b dark:border-gray-700
              transition-colors hover:bg-gray-100 dark:hover:bg-gray-700
              ${selectedId === agent.id ? 'bg-blue-50 dark:bg-blue-900/30' : ''}
            `}
          >
            <div className="flex items-center gap-2 mb-1">
              <span
                className={`w-2 h-2 rounded-full ${statusColors[agent.status] || 'bg-gray-400'}`}
              />
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {agent.status}
              </span>
            </div>
            <p className="text-sm text-gray-800 dark:text-gray-200 line-clamp-2">
              {agent.task_description || 'No description'}
            </p>
            {agent.created_at && (
              <p className="text-xs text-gray-400 mt-1">
                {new Date(agent.created_at).toLocaleString()}
              </p>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
