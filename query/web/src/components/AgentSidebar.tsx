/**
 * Sidebar component showing list of BOSS agents.
 */

import type { AgentListItem } from '../types/api';

interface AgentSidebarProps {
  agents: AgentListItem[];
  selectedId: string | null;
  onSelect: (agentId: string) => void;
  loading: boolean;
  error: string | null;
}

const statusColors: Record<string, string> = {
  pending: 'bg-yellow-400',
  analyzing: 'bg-yellow-400',
  in_progress: 'bg-blue-400',
  waiting: 'bg-blue-400',
  completed: 'bg-green-400',
  failed: 'bg-red-400',
  blocked: 'bg-orange-400',
};

export function AgentSidebar({ agents, selectedId, onSelect, loading, error }: AgentSidebarProps) {
  if (loading) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading agents...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4">
        <div className="text-red-500 text-sm mb-2">Failed to connect to API</div>
        <div className="text-xs text-gray-400 break-words">{error}</div>
        <div className="mt-3 text-xs text-gray-500">
          Make sure the API server is running on port 8000.
        </div>
      </div>
    );
  }

  if (agents.length === 0) {
    return (
      <div className="p-4 text-gray-500">
        <div className="mb-2">No agents found.</div>
        <div className="text-xs">
          Start a task to see agents here:
          <pre className="mt-2 p-2 bg-gray-100 dark:bg-gray-700 rounded text-xs overflow-x-auto">
docker compose exec app python main.py run "your task"
          </pre>
        </div>
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
              {agent.was_restarted && (
                <span
                  className="text-[10px] font-semibold rounded px-1.5 py-0.5 bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-300"
                  title={`Restarted ${agent.restart_count} time${agent.restart_count === 1 ? '' : 's'}`}
                >
                  restarted ×{agent.restart_count}
                </span>
              )}
              {agent.was_hang_restarted && (
                <span
                  className="text-[10px] font-semibold rounded px-1.5 py-0.5 bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300"
                  title={`Hang-recovery restart ${agent.hang_restart_count} time${agent.hang_restart_count === 1 ? '' : 's'}`}
                >
                  hang-recovered ×{agent.hang_restart_count}
                </span>
              )}
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
