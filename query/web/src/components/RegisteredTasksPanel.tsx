/**
 * Panel for displaying registered tasks in the global deduplication registry.
 * Shows all tasks that have been registered during task decomposition.
 */

import type { RegisteredTask } from '../types/api';

interface RegisteredTasksPanelProps {
  tasks: RegisteredTask[];
  total: number;
  loading: boolean;
  onRefresh: () => void;
}

export function RegisteredTasksPanel({
  tasks,
  total,
  loading,
  onRefresh,
}: RegisteredTasksPanelProps) {
  if (loading && tasks.length === 0) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading registered tasks...</div>
      </div>
    );
  }

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-800 dark:text-white">
          Registered Tasks
        </h3>
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-500 dark:text-gray-400">
            {total} tasks
          </span>
          <button
            onClick={onRefresh}
            disabled={loading}
            className="px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Description */}
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Tasks in the deduplication registry. Duplicate task descriptions are
        filtered during decomposition to prevent redundant work.
      </p>

      {/* Task List */}
      {tasks.length === 0 ? (
        <div className="text-gray-500 dark:text-gray-400 text-sm">
          No tasks registered yet. Tasks are registered during task decomposition.
        </div>
      ) : (
        <div className="space-y-2">
          {tasks.map((task) => (
            <div
              key={task.task_key}
              className="bg-gray-50 dark:bg-gray-800 p-3 rounded border-l-2 border-blue-400"
            >
              <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">
                {task.task_description}
              </p>
              <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
                <span title="Task Key (SHA256 hash)">
                  Key: <code className="bg-gray-200 dark:bg-gray-700 px-1 rounded">{task.task_key}</code>
                </span>
                <span title="Registered by Agent">
                  Agent: <code className="bg-gray-200 dark:bg-gray-700 px-1 rounded">{task.registered_by.slice(0, 8)}...</code>
                </span>
                {task.parent_id && (
                  <span title="Parent Agent">
                    Parent: <code className="bg-gray-200 dark:bg-gray-700 px-1 rounded">{task.parent_id.slice(0, 8)}...</code>
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
