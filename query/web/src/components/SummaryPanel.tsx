/**
 * Panel for displaying agent summary information when a node is selected.
 * Shows task, complexity reasoning, config, and subtasks/tool info.
 */

import type { AgentSummary } from '../types/api';

interface SummaryPanelProps {
  summary: AgentSummary | null;
  loading: boolean;
}

const statusColors: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
  analyzing: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  in_progress: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  waiting: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
};

const roleIcons: Record<string, string> = {
  boss: '👑',
  manager: '📋',
  worker: '⚙️',
  pending: '⏳',
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
        {title}
      </h3>
      {children}
    </div>
  );
}

export function SummaryPanel({ summary, loading }: SummaryPanelProps) {
  if (loading) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading summary...</div>
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="p-4 text-gray-500">
        Select an agent node to view its summary
      </div>
    );
  }

  const roleIcon = roleIcons[summary.role.toLowerCase()] || '❓';
  const statusClass = statusColors[summary.status.toLowerCase()] || statusColors.pending;

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      {/* Header with role and status */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-2xl">{roleIcon}</span>
          <span className="font-semibold text-gray-800 dark:text-white capitalize">
            {summary.role}
          </span>
        </div>
        <span className={`px-2 py-1 text-xs rounded-full ${statusClass}`}>
          {summary.status}
        </span>
      </div>

      {/* Task Description (Objective) */}
      <Section title="Objective">
        <p className="text-sm text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-800 p-2 rounded">
          {summary.task_description || 'No task assigned'}
        </p>
      </Section>

      {/* Budget Information */}
      {summary.budget && (
        <Section title="Budget">
          <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded space-y-2">
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Current Balance:</span>
              <span className="text-sm font-semibold text-green-600 dark:text-green-400">
                ${summary.budget.current_budget.toFixed(1)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Initial Allocation:</span>
              <span className="text-sm text-gray-700 dark:text-gray-300">
                ${summary.budget.initial_budget.toFixed(1)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Spent:</span>
              <span className={`text-sm ${
                summary.budget.spent > 0
                  ? 'text-orange-600 dark:text-orange-400'
                  : 'text-gray-700 dark:text-gray-300'
              }`}>
                ${summary.budget.spent.toFixed(1)}
              </span>
            </div>
            {/* Progress bar */}
            <div className="mt-2">
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                <div
                  className="bg-green-500 h-2 rounded-full transition-all"
                  style={{
                    width: `${Math.min(100, (summary.budget.current_budget / summary.budget.initial_budget) * 100)}%`
                  }}
                />
              </div>
              <div className="flex justify-between text-xs text-gray-500 mt-1">
                <span>0</span>
                <span>{summary.budget.source || 'budget'}</span>
                <span>${summary.budget.initial_budget.toFixed(0)}</span>
              </div>
            </div>
          </div>
        </Section>
      )}

      {/* Complexity Evaluation (for non-BOSS agents) */}
      {summary.complexity && (
        <Section title="Complexity Evaluation">
          <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-gray-500">Result:</span>
              <span className={`px-2 py-0.5 text-xs rounded ${
                summary.complexity === 'simple'
                  ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                  : 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300'
              }`}>
                {summary.complexity.toUpperCase()}
              </span>
              <span className="text-xs text-gray-500">
                → became {summary.complexity === 'simple' ? 'WORKER' : 'MANAGER'}
              </span>
            </div>
            {summary.complexity_reasoning && (
              <p className="text-xs text-gray-600 dark:text-gray-400 italic">
                "{summary.complexity_reasoning}"
              </p>
            )}
          </div>
        </Section>
      )}

      {/* Worker Tool (for WORKER agents) */}
      {summary.worker_tool && (
        <Section title="Worker Tool">
          <div className="flex items-center gap-2 bg-gray-50 dark:bg-gray-800 p-2 rounded">
            <span className="text-lg">
              {summary.worker_tool === 'claude_code' ? '🤖' : '🔧'}
            </span>
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {summary.worker_tool === 'claude_code' ? 'Claude Code' : 'OpenHands'}
            </span>
          </div>
        </Section>
      )}

      {/* Subtasks (for MANAGER agents) */}
      {summary.subtasks.length > 0 && (
        <Section title={`Subtasks (${summary.subtasks.length})`}>
          <div className="space-y-2">
            {summary.subtasks.map((subtask, idx) => (
              <div
                key={idx}
                className="bg-gray-50 dark:bg-gray-800 p-2 rounded border-l-2 border-blue-400"
              >
                <p className="text-sm text-gray-700 dark:text-gray-300">
                  {subtask.description}
                </p>
                {subtask.child_status && (
                  <div className="mt-1 flex items-center gap-2">
                    <span className={`px-1.5 py-0.5 text-xs rounded ${
                      statusColors[subtask.child_status] || statusColors.pending
                    }`}>
                      {subtask.child_status}
                    </span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Task Queue (pending tasks) */}
      {summary.queue_size > 0 && (
        <Section title={`Task Queue (${summary.queue_size})`}>
          <div className="space-y-2">
            {summary.task_queue.map((task, idx) => (
              <div
                key={idx}
                className="bg-yellow-50 dark:bg-yellow-900/20 p-2 rounded border-l-2 border-yellow-400 flex items-center gap-2"
              >
                <span className="text-yellow-600 dark:text-yellow-400">
                  {idx + 1}.
                </span>
                <p className="text-sm text-gray-700 dark:text-gray-300 flex-1">
                  {task.description}
                </p>
                {task.priority > 0 && (
                  <span className="px-1.5 py-0.5 text-xs bg-orange-100 dark:bg-orange-900/30 text-orange-800 dark:text-orange-300 rounded">
                    P{task.priority}
                  </span>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Configuration */}
      {summary.config_strategy && (
        <Section title="Configuration">
          <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-gray-500">Strategy:</span>
              <span className="text-xs text-gray-700 dark:text-gray-300">
                {summary.config_strategy}
              </span>
            </div>
            {Object.keys(summary.config_details).length > 0 && (
              <pre className="text-xs text-gray-600 dark:text-gray-400 overflow-x-auto">
                {JSON.stringify(summary.config_details, null, 2)}
              </pre>
            )}
          </div>
        </Section>
      )}

      {/* Result (if completed) */}
      {summary.result && (
        <Section title="Result">
          <div className="bg-green-50 dark:bg-green-900/20 p-2 rounded border-l-2 border-green-500">
            <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
              {summary.result}
            </p>
          </div>
        </Section>
      )}

      {/* Error (if failed) */}
      {summary.error_message && (
        <Section title="Error">
          <div className="bg-red-50 dark:bg-red-900/20 p-2 rounded border-l-2 border-red-500">
            <p className="text-sm text-red-700 dark:text-red-300">
              {summary.error_message}
            </p>
          </div>
        </Section>
      )}
    </div>
  );
}
